from __future__ import annotations

from fastapi import FastAPI

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


def main() -> None:
    import uvicorn

    from opsgentic.config import get_settings

    uvicorn.run(app, host="0.0.0.0", port=8080, log_level=get_settings().log_level.lower())


if __name__ == "__main__":
    main()
