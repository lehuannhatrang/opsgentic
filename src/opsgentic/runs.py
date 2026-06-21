from __future__ import annotations

from typing import Optional

import psycopg
from psycopg.types.json import Jsonb

from opsgentic.config import get_settings

# Lightweight run-status store, separate from the LangGraph checkpoint tables. It records the
# job lifecycle the checkpoint cannot: 'queued' (before the worker starts) and 'running', plus
# terminal status/pr_url/error. The API merges this with the checkpoint snapshot for polling.

_DDL = """
CREATE TABLE IF NOT EXISTS opsgentic_runs (
    thread_id  text PRIMARY KEY,
    status     text NOT NULL,
    alert      jsonb,
    pr_url     text,
    error      text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
)
"""


def _conn():
    return psycopg.connect(get_settings().database_url, autocommit=True)


def ensure_schema() -> None:
    with _conn() as c:
        c.execute(_DDL)


def create(thread_id: str, alert: dict, status: str = "queued") -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO opsgentic_runs (thread_id, status, alert) VALUES (%s, %s, %s) "
            "ON CONFLICT (thread_id) DO UPDATE SET status = EXCLUDED.status, "
            "alert = EXCLUDED.alert, updated_at = now()",
            (thread_id, status, Jsonb(alert or {})),
        )


def set_status(thread_id: str, status: str, *, pr_url: Optional[str] = None, error: Optional[str] = None) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE opsgentic_runs SET status = %s, pr_url = COALESCE(%s, pr_url), "
            "error = COALESCE(%s, error), updated_at = now() WHERE thread_id = %s",
            (status, pr_url, error, thread_id),
        )


def get(thread_id: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT thread_id, status, pr_url, error, created_at, updated_at "
            "FROM opsgentic_runs WHERE thread_id = %s",
            (thread_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "thread_id": row[0],
        "status": row[1],
        "pr_url": row[2],
        "error": row[3],
        "created_at": row[4].isoformat(),
        "updated_at": row[5].isoformat(),
    }
