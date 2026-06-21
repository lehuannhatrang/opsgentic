from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def main() -> None:
    """Run the Procrastinate worker that consumes queued runs and drives the graph.

    Bootstraps the queue schema (idempotent), then runs the worker in the foreground.
    Deploy as a separate K8s Deployment with this entrypoint (command: ["opsgentic-worker"]).
    """
    logging.basicConfig(level=logging.INFO)
    from opsgentic import tasks
    from opsgentic.config import get_settings

    if not get_settings().database_url:
        raise SystemExit("DATABASE_URL is required for the worker (Postgres-backed queue).")

    tasks.ensure_schema()
    with tasks.app.open():
        logger.info("Starting opsgentic worker")
        tasks.app.run_worker(install_signal_handlers=True)


if __name__ == "__main__":
    main()
