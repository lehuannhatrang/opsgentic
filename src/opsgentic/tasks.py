from __future__ import annotations

from procrastinate import App, PsycopgConnector

from opsgentic.config import get_settings

# Procrastinate task queue backed by the same Postgres used for checkpoints — no extra broker.
# The API defers jobs (defer_async); a separate worker Deployment consumes and runs the graph.
# conninfo may be empty when DATABASE_URL is unset (local/dev); the connector is only opened when
# the queue is actually used (see runner.queue_enabled / main.lifespan).
app = App(connector=PsycopgConnector(conninfo=get_settings().database_url or ""))


@app.task(name="run_alert", queue="remediation")
def run_alert(thread_id: str, alert_payload: dict, auto_approve: bool | None = None) -> None:
    from opsgentic import runner

    runner.run_and_track(thread_id, alert_payload, auto_approve=auto_approve)


@app.task(name="resume_run", queue="remediation")
def resume_run(thread_id: str, decision: str) -> None:
    from opsgentic import runner

    runner.resume_and_track(thread_id, decision)


def ensure_schema() -> None:
    """Create the run-status table and the Procrastinate schema if missing (idempotent).

    Uses a plain psycopg connection and `get_schema()` (a static SQL string), so it does not
    require opening the connector pool. Guarded by to_regclass so it runs at most once. Called
    from both the worker startup and the API lifespan so either can bootstrap a fresh database."""
    import psycopg

    from opsgentic import runs

    runs.ensure_schema()
    with psycopg.connect(get_settings().database_url, autocommit=True) as c:
        if c.execute("SELECT to_regclass('procrastinate_jobs')").fetchone()[0] is None:
            c.execute(app.schema_manager.get_schema())
