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
