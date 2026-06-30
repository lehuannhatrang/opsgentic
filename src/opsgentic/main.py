from __future__ import annotations

import html
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse

from opsgentic import runner
from opsgentic.config import get_settings
from opsgentic.triggers import github as gh
from opsgentic.triggers import normalize

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Queue mode: bootstrap the schema (idempotent) and open the connector pool so endpoints
    # can defer jobs. Without a queue (no DATABASE_URL) the API runs the graph in-process.
    if runner.queue_enabled():
        import asyncio

        from opsgentic import tasks

        await asyncio.to_thread(tasks.ensure_schema)
        async with tasks.app.open_async():
            yield
    else:
        yield


app = FastAPI(title="opsgentic", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/webhook/grafana", status_code=202)
async def grafana_webhook(payload: dict) -> dict:
    return await runner.enqueue(normalize.from_grafana(payload))


@app.post("/chat", status_code=202)
async def chat(payload: dict) -> dict:
    return await runner.enqueue(normalize.from_chat(payload))


@app.post("/webhook/github")
async def github_webhook(request: Request) -> Response:
    """Handle GitHub `issue_comment` webhooks for the PR comment agent. Verifies the HMAC
    signature, filters to PR comments that warrant a response, dedups, and enqueues."""
    settings = get_settings()
    body = await request.body()
    secret = settings.github_webhook_secret
    if secret:
        if not gh.verify_signature(body, request.headers.get("X-Hub-Signature-256"), secret):
            return Response(status_code=401)
    else:
        logger.warning("GITHUB_WEBHOOK_SECRET not set; accepting webhook unverified (dev only)")

    if request.headers.get("X-GitHub-Event") != "issue_comment":
        return Response(status_code=204)
    event = gh.parse_comment_event(json.loads(body or b"{}"))
    if event is None:
        return Response(status_code=204)

    handle = settings.github_agent_handle
    if gh.is_self(event, handle):
        return Response(status_code=204)

    if runner.queue_enabled():
        from opsgentic import runs

        try:
            if runs.is_comment_processed(event["comment_id"]):
                return Response(status_code=204)
        except Exception as exc:
            logger.warning("dedup check failed: %s", exc)

    try:
        from opsgentic.gitops import pr as prmod

        comments = prmod.list_pr_comments(
            event["owner"], event["repo"], event["pr_number"],
            limit=settings.pr_responder_max_comments, host=event.get("host", "github.com"),
        )
    except Exception as exc:  # mention-only fallback if the comment list is unavailable
        logger.warning("list_pr_comments failed: %s", exc)
        comments = []

    if not gh.should_respond(event, comments, handle):
        return Response(status_code=204)

    if runner.queue_enabled():
        from opsgentic import runs

        try:
            runs.mark_comment_processed(event["comment_id"], event.get("pr_url"))
        except Exception as exc:
            logger.warning("mark_comment_processed failed: %s", exc)

    await runner.enqueue_comment(event)
    return Response(status_code=202)


@app.get("/runs")
def list_runs(limit: int = 50) -> list[dict]:
    return runner.list_runs(limit)


@app.get("/runs/{thread_id}")
def get_run(thread_id: str) -> dict:
    return runner.get_run(thread_id)


@app.get("/graph")
def system_graph() -> dict:
    from opsgentic import graphview

    return graphview.build_system_graph()


@app.get("/graph/tools/{server}")
def graph_server_tools(server: str) -> dict:
    from opsgentic import graphview

    return graphview.list_server_tools(server)


@app.get("/runs/{thread_id}/graph")
def run_graph(thread_id: str) -> dict:
    from opsgentic import graphview

    return graphview.build_run_graph(thread_id)


@app.post("/runs/{thread_id}/approve", status_code=202)
async def approve_run(thread_id: str) -> dict:
    return await runner.enqueue_resume(thread_id, "approve")


@app.post("/runs/{thread_id}/reject", status_code=202)
async def reject_run(thread_id: str) -> dict:
    return await runner.enqueue_resume(thread_id, "reject")


@app.get("/ui/{thread_id}", response_class=HTMLResponse)
def ui(thread_id: str) -> str:
    return _render(runner.get_run(thread_id))


def _render(snap: dict) -> str:
    state = snap["state"]
    plan = state.get("remediation_plan") or {}
    tid = snap["thread_id"]
    status = snap.get("status") or state.get("execution_status", "unknown")
    hypothesis = html.escape(state.get("hypothesis") or "")
    pr_url = state.get("pr_url")

    if snap["awaiting_approval"]:
        controls = (
            f"<button onclick=\"act('approve')\">Approve</button> "
            f"<button onclick=\"act('reject')\">Reject</button>"
        )
    elif pr_url:
        controls = f"<p>PR: <a href=\"{html.escape(pr_url)}\">{html.escape(pr_url)}</a></p>"
    else:
        controls = "<p>No action required.</p>"

    rows = "".join(
        f"<tr><th align=left>{html.escape(k)}</th><td>{html.escape(str(plan.get(k, '')))}</td></tr>"
        for k in ("summary", "target_repo", "file_path", "risk")
    )

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>opsgentic run {html.escape(tid)}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:720px;margin:2rem auto;padding:0 1rem}}
table{{border-collapse:collapse;width:100%}} th,td{{padding:4px 8px;border-bottom:1px solid #ddd}}
pre{{background:#f5f5f5;padding:8px;white-space:pre-wrap}} button{{padding:6px 14px;margin-right:6px}}</style>
</head><body>
<h2>Remediation review</h2>
<p>Run <code>{html.escape(tid)}</code> — status: <b>{html.escape(status)}</b></p>
<h3>Root cause hypothesis</h3><pre>{hypothesis}</pre>
<h3>Remediation plan</h3><table>{rows}</table>
<p>{controls}</p>
<script>
async function act(a){{
  await fetch('/runs/{html.escape(tid)}/' + a, {{method: 'POST'}});
  location.reload();
}}
</script>
</body></html>"""


def main() -> None:
    import uvicorn

    from opsgentic.config import get_settings

    uvicorn.run(app, host="0.0.0.0", port=8080, log_level=get_settings().log_level.lower())


if __name__ == "__main__":
    main()
