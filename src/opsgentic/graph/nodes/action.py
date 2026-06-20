from __future__ import annotations

from langchain_core.messages import AIMessage

from opsgentic.gitops.pr import create_pull_request
from opsgentic.graph.state import MachineState


def action_node(state: MachineState) -> dict:
    """Runs only AFTER human approval (interrupt_before gate)."""
    plan = state.get("remediation_plan")
    if not plan:
        return {
            "execution_status": "failed",
            "messages": [AIMessage(content="No remediation plan to apply.")],
        }

    pr_url = create_pull_request(plan)
    return {
        "pr_url": pr_url,
        "execution_status": "applied",
        "messages": [AIMessage(content=f"Opened remediation PR: {pr_url}")],
    }
