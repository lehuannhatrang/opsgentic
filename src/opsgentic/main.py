from __future__ import annotations

import html

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from opsgentic import runner
from opsgentic.triggers import normalize

app = FastAPI(title="opsgentic")


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/webhook/grafana")
def grafana_webhook(payload: dict) -> dict:
    return runner.start_run(normalize.from_grafana(payload))


@app.post("/chat")
def chat(payload: dict) -> dict:
    return runner.start_run(normalize.from_chat(payload))


@app.get("/runs/{thread_id}")
def get_run(thread_id: str) -> dict:
    return runner.snapshot(thread_id)


@app.post("/runs/{thread_id}/approve")
def approve_run(thread_id: str) -> dict:
    return runner.approve(thread_id)


@app.post("/runs/{thread_id}/reject")
def reject_run(thread_id: str) -> dict:
    return runner.reject(thread_id)


@app.get("/ui/{thread_id}", response_class=HTMLResponse)
def ui(thread_id: str) -> str:
    return _render(runner.snapshot(thread_id))


def _render(snap: dict) -> str:
    state = snap["state"]
    plan = state.get("remediation_plan") or {}
    tid = snap["thread_id"]
    status = state.get("execution_status", "unknown")
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
