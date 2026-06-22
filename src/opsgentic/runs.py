from __future__ import annotations

from typing import Optional

import psycopg
from psycopg.types.json import Jsonb

from opsgentic.config import get_settings

# Lightweight run-status store, separate from the LangGraph checkpoint tables. It records the
# job lifecycle the checkpoint cannot ('queued'/'running'), the terminal status/pr_url/error, and
# a small `summary` (alert + resolved workload/repo) so the console list view is meaningful.

_DDL = """
CREATE TABLE IF NOT EXISTS opsgentic_runs (
    thread_id  text PRIMARY KEY,
    status     text NOT NULL,
    alert      jsonb,
    summary    jsonb,
    pr_url     text,
    error      text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
)
"""

_COLS = "thread_id, status, summary, pr_url, error, created_at, updated_at"


def _conn():
    return psycopg.connect(get_settings().database_url, autocommit=True)


def ensure_schema() -> None:
    with _conn() as c:
        c.execute(_DDL)
        c.execute("ALTER TABLE opsgentic_runs ADD COLUMN IF NOT EXISTS summary jsonb")  # upgrade existing


def _summary_from_alert(alert: dict) -> dict:
    """Initial row summary from the trigger (known at enqueue, before resolution)."""
    alert = alert or {}
    labels = alert.get("labels") or {}
    return {
        "title": alert.get("title"),
        "source": alert.get("source"),
        "severity": alert.get("severity"),
        "namespace": labels.get("namespace"),
        "service": labels.get("app") or labels.get("workload") or labels.get("deployment"),
        "repo": None,
    }


def _row(row) -> dict:
    return {
        "thread_id": row[0],
        "status": row[1],
        "summary": row[2] or {},
        "pr_url": row[3],
        "error": row[4],
        "created_at": row[5].isoformat(),
        "updated_at": row[6].isoformat(),
    }


def create(thread_id: str, alert: dict, status: str = "queued") -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO opsgentic_runs (thread_id, status, alert, summary) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (thread_id) DO UPDATE SET status = EXCLUDED.status, "
            "alert = EXCLUDED.alert, summary = EXCLUDED.summary, updated_at = now()",
            (thread_id, status, Jsonb(alert or {}), Jsonb(_summary_from_alert(alert))),
        )


def set_status(thread_id: str, status: str, *, pr_url: Optional[str] = None, error: Optional[str] = None) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE opsgentic_runs SET status = %s, pr_url = COALESCE(%s, pr_url), "
            "error = COALESCE(%s, error), updated_at = now() WHERE thread_id = %s",
            (status, pr_url, error, thread_id),
        )


def set_summary(thread_id: str, extra: dict) -> None:
    """Merge non-null fields into the row summary (e.g. resolved service/repo after the run)."""
    extra = {k: v for k, v in (extra or {}).items() if v is not None}
    if not extra:
        return
    with _conn() as c:
        c.execute(
            "UPDATE opsgentic_runs SET summary = COALESCE(summary, '{}'::jsonb) || %s, "
            "updated_at = now() WHERE thread_id = %s",
            (Jsonb(extra), thread_id),
        )


def list_recent(limit: int = 50) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            f"SELECT {_COLS} FROM opsgentic_runs ORDER BY updated_at DESC LIMIT %s", (limit,)
        ).fetchall()
    return [_row(r) for r in rows]


def get(thread_id: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            f"SELECT {_COLS} FROM opsgentic_runs WHERE thread_id = %s", (thread_id,)
        ).fetchone()
    return _row(row) if row else None
