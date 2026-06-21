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


# --- read (polling) -------------------------------------------------------------------------

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
