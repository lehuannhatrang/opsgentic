from __future__ import annotations

import uuid

from langgraph.checkpoint.memory import MemorySaver

from opsgentic.graph.builder import build_app

# M1: in-memory checkpointer (single process). M3 switches to PostgresSaver (durable).
_checkpointer = MemorySaver()
_app = build_app(_checkpointer)


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def start_run(alert_payload: dict, thread_id: str | None = None) -> dict:
    thread_id = thread_id or str(uuid.uuid4())
    initial = {"alert_payload": alert_payload, "execution_status": "pending"}
    _app.invoke(initial, _config(thread_id))   # stops before 'action' (interrupt_before)
    return snapshot(thread_id)


def approve(thread_id: str) -> dict:
    cfg = _config(thread_id)
    _app.update_state(cfg, {"execution_status": "approved"})
    _app.invoke(None, cfg)                      # resume -> action node opens the PR
    return snapshot(thread_id)


def reject(thread_id: str) -> dict:
    cfg = _config(thread_id)
    _app.update_state(cfg, {"execution_status": "rejected"})
    return snapshot(thread_id)


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
