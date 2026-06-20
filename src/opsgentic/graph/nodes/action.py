from __future__ import annotations

import logging

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from opsgentic.gitops.pr import create_pull_request
from opsgentic.graph.state import MachineState

logger = logging.getLogger(__name__)


def action_node(state: MachineState, config: RunnableConfig) -> dict:
    """Runs only AFTER human approval (interrupt_before gate)."""
    plan = state.get("remediation_plan")
    if not plan:
        return {
            "execution_status": "failed",
            "messages": [AIMessage(content="No remediation plan to apply.")],
        }

    run_id = (config or {}).get("configurable", {}).get("thread_id", "unknown")
    try:
        pr_url = create_pull_request(
            plan,
            run_id=run_id,
            hypothesis=state.get("hypothesis", ""),
            validation_report=state.get("validation_report"),
        )
    except Exception as exc:
        logger.warning("PR creation failed: %s", exc)
        return {
            "execution_status": "failed",
            "messages": [AIMessage(content=f"Failed to open remediation PR: {exc}")],
        }

    return {
        "pr_url": pr_url,
        "execution_status": "applied",
        "messages": [AIMessage(content=f"Opened remediation PR (awaiting merge & GitOps sync): {pr_url}")],
    }
