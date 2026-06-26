from __future__ import annotations

import logging
import uuid

from langgraph.checkpoint.memory import MemorySaver

from opsgentic.config import get_settings
from opsgentic.graph.builder import build_app

logger = logging.getLogger(__name__)


def _build_checkpointer():
    """Durable PostgresSaver when DATABASE_URL is set; otherwise in-memory."""
    settings = get_settings()
    if not settings.database_url:
        return MemorySaver()
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        pool = ConnectionPool(
            conninfo=settings.database_url,
            max_size=settings.db_pool_max_size,
            open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        )
        pool.open()
        saver = PostgresSaver(pool)
        saver.setup()
        logger.info("Using PostgresSaver for durable checkpoints")
        return saver
    except Exception as exc:  # missing driver / unreachable DB -> safe dev fallback
        logger.warning("PostgresSaver unavailable (%s); falling back to MemorySaver", exc)
        return MemorySaver()


_checkpointer = _build_checkpointer()
_app = build_app(_checkpointer)


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def queue_enabled() -> bool:
    """A Postgres queue/worker is used only when DATABASE_URL is set; otherwise the API runs
    the graph synchronously in-process (local/dev)."""
    return bool(get_settings().database_url)


# --- synchronous graph execution (worker tasks / CLI / no-queue fallback) -------------------

def execute_run(alert_payload: dict, thread_id: str | None = None, auto_approve: bool | None = None) -> dict:
    if auto_approve is None:
        auto_approve = get_settings().auto_approve
    thread_id = thread_id or str(uuid.uuid4())
    cfg = _config(thread_id)
    initial = {"alert_payload": alert_payload, "execution_status": "pending"}
    _app.invoke(initial, cfg)                   # stops before 'action' (interrupt_before)
    if auto_approve and "action" in _app.get_state(cfg).next:
        _app.invoke(None, cfg)                  # auto-resume -> action opens the PR
    return snapshot(thread_id)


def execute_approve(thread_id: str) -> dict:
    cfg = _config(thread_id)
    _app.update_state(cfg, {"execution_status": "approved"})
    _app.invoke(None, cfg)                      # resume -> action node opens the PR
    return snapshot(thread_id)


def execute_reject(thread_id: str) -> dict:
    cfg = _config(thread_id)
    # Attribute to 'action' so the graph ends (action -> END) without running it.
    _app.update_state(cfg, {"execution_status": "rejected"}, as_node="action")
    return snapshot(thread_id)


def _derive_status(snap: dict) -> str:
    if snap.get("awaiting_approval"):
        return "awaiting_approval"
    return snap["state"].get("execution_status") or "unknown"


def _run_summary(snap: dict) -> dict:
    """Resolved workload/repo from the run, to enrich the opsgentic_runs row summary."""
    st = snap.get("state", {}) or {}
    svc = st.get("service_ref") or {}
    tgt = st.get("gitops_target") or {}
    return {"service": svc.get("name"), "namespace": svc.get("namespace"), "repo": tgt.get("slug")}


def run_and_track(thread_id: str, alert_payload: dict, auto_approve: bool | None = None) -> dict:
    """Worker entry: run the graph and record the lifecycle in opsgentic_runs."""
    from opsgentic import runs

    runs.set_status(thread_id, "running")
    try:
        snap = execute_run(alert_payload, thread_id=thread_id, auto_approve=auto_approve)
    except Exception as exc:
        runs.set_status(thread_id, "failed", error=str(exc))
        raise
    runs.set_status(thread_id, _derive_status(snap), pr_url=snap["state"].get("pr_url"))
    runs.set_summary(thread_id, _run_summary(snap))
    return snap


def resume_and_track(thread_id: str, decision: str) -> dict:
    from opsgentic import runs

    runs.set_status(thread_id, "running")
    try:
        snap = execute_approve(thread_id) if decision == "approve" else execute_reject(thread_id)
    except Exception as exc:
        runs.set_status(thread_id, "failed", error=str(exc))
        raise
    runs.set_status(thread_id, _derive_status(snap), pr_url=snap["state"].get("pr_url"))
    runs.set_summary(thread_id, _run_summary(snap))
    return snap


# --- async enqueue (API) --------------------------------------------------------------------

async def enqueue(alert_payload: dict, thread_id: str | None = None, auto_approve: bool | None = None) -> dict:
    """Queue the run and return immediately with polling metadata. Without a queue (no
    DATABASE_URL), run synchronously off the event loop and return the full snapshot."""
    import asyncio

    thread_id = thread_id or str(uuid.uuid4())
    if not queue_enabled():
        return await asyncio.to_thread(execute_run, alert_payload, thread_id, auto_approve)
    from opsgentic import runs
    from opsgentic.tasks import run_alert

    await asyncio.to_thread(runs.create, thread_id, alert_payload, "queued")
    await run_alert.defer_async(thread_id=thread_id, alert_payload=alert_payload, auto_approve=auto_approve)
    return {"thread_id": thread_id, "status": "queued", "poll_url": f"/runs/{thread_id}"}


async def enqueue_resume(thread_id: str, decision: str) -> dict:
    import asyncio

    if not queue_enabled():
        fn = execute_approve if decision == "approve" else execute_reject
        return await asyncio.to_thread(fn, thread_id)
    from opsgentic import runs
    from opsgentic.tasks import resume_run

    await asyncio.to_thread(runs.set_status, thread_id, "queued")
    await resume_run.defer_async(thread_id=thread_id, decision=decision)
    return {"thread_id": thread_id, "status": "queued", "poll_url": f"/runs/{thread_id}"}


# --- PR comment handling --------------------------------------------------------------------

def handle_comment_and_track(event: dict) -> None:
    """Worker entry for a PR comment: assemble context, run the responder, optionally commit
    the agreed edit to the PR branch, post the reply, and record the interaction as a run row
    so it shows in the console."""
    from opsgentic.conversation import context as conv_context
    from opsgentic.conversation import responder
    from opsgentic.gitops import pr as prmod
    from opsgentic.gitops.remediator import edits_to_ops

    thread_id = f"prcomment-{event.get('comment_id')}"
    ctx = conv_context.assemble_pr_context(event)
    resp, tool_calls = responder.respond(ctx, event)
    reply = resp.reply or "(no reply)"
    committed = False

    if resp.kind == "agree_and_edit" and resp.edits and ctx["pr_info"].get("branch"):
        try:
            ops = edits_to_ops(resp.edits)
            if ops:
                prmod.update_remediation_pr(
                    ctx["plan"], ctx["pr_info"], edits=ops,
                    reason="applied human suggestion from PR comment",
                    alert=ctx.get("alert"), workload=ctx.get("service_ref"),
                )
                committed = True
        except Exception as exc:
            logger.warning("failed to commit suggested edits: %s", exc)
            reply += ("\n\n_(I agreed with the change but couldn't commit it automatically; "
                      "please apply it manually.)_")

    try:
        prmod.post_pr_comment(
            event["owner"], event["repo"], event["pr_number"],
            reply + prmod.AGENT_FOOTER, host=event.get("host", "github.com"),
        )
    except Exception as exc:
        logger.warning("failed to post PR reply: %s", exc)

    _record_comment_run(thread_id, event, ctx, resp, reply, tool_calls, committed)


def _record_comment_run(thread_id, event, ctx, resp, reply, tool_calls, committed) -> None:
    """Persist the comment interaction to opsgentic_runs for the console (best-effort)."""
    if not queue_enabled():
        return
    from opsgentic import runs

    svc = ctx.get("service_ref") or {}
    summary = {
        "source": "github-comment",
        "title": ctx.get("pr_title") or f"PR #{event.get('pr_number')} comment",
        "repo": f"{event.get('owner')}/{event.get('repo')}",
        "namespace": svc.get("namespace"),
        "service": svc.get("name"),
        "kind": resp.kind,
        "author": event.get("author_login"),
        "comment": event.get("comment_body"),
        "reply": reply,
        "tool_calls": tool_calls,
    }
    status = "applied" if committed else "completed"
    try:
        runs.upsert_comment_run(thread_id, status=status, summary=summary, pr_url=event.get("pr_url"))
    except Exception as exc:
        logger.warning("failed to record comment run: %s", exc)


async def enqueue_comment(event: dict) -> dict:
    """Queue a PR comment for handling (queue mode), or run it inline (no DB / dev)."""
    import asyncio

    if not queue_enabled():
        await asyncio.to_thread(handle_comment_and_track, event)
        return {"status": "processed", "comment_id": event.get("comment_id")}
    from opsgentic.tasks import handle_pr_comment

    await handle_pr_comment.defer_async(event=event)
    return {"status": "queued", "comment_id": event.get("comment_id")}


# --- read (polling) -------------------------------------------------------------------------

def list_runs(limit: int = 50) -> list[dict]:
    """Recent runs from opsgentic_runs (queue mode only; [] without a DB)."""
    if not queue_enabled():
        return []
    from opsgentic import runs

    try:
        return runs.list_recent(limit)
    except Exception:
        return []


def get_run(thread_id: str) -> dict:
    """Merge the durable run status (opsgentic_runs) with the checkpoint snapshot."""
    snap = snapshot(thread_id)
    meta = None
    if queue_enabled():
        from opsgentic import runs

        try:
            meta = runs.get(thread_id)
        except Exception:
            meta = None
    out = {**snap, "status": (meta or {}).get("status") or _derive_status(snap)}
    out["summary"] = (meta or {}).get("summary") or {}
    if meta and meta.get("error"):
        out["error"] = meta["error"]
    return out


def snapshot(thread_id: str) -> dict:
    state = _app.get_state(_config(thread_id))
    values = dict(state.values)
    values.pop("messages", None)                # trim the response payload
    return {
        "thread_id": thread_id,
        "next": list(state.next),
        "awaiting_approval": "action" in state.next,
        "state": values,
    }
