from __future__ import annotations

import logging

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from opsgentic.gitops.pr import (
    create_pull_request,
    find_open_remediation_pr,
    proposed_diff,
    update_remediation_pr,
)
from opsgentic.gitops.remediator import generate_edits, reassess_edits
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
    alert = state.get("alert_payload")
    workload = state.get("service_ref")
    tool_calls: list = []
    try:
        existing = find_open_remediation_pr(plan, alert)
        if existing:
            # Re-fire: assess what the open PR already proposes; commit an incremental fix only if
            # it is insufficient, otherwise just comment. Avoids divergent commits on every re-fire.
            sufficient, reason, edits = reassess_edits(state, proposed_diff(plan, existing))
            pr_url = update_remediation_pr(plan, existing, edits=edits, reason=reason,
                                           alert=alert, workload=workload)
            msg = (
                f"Re-fired alert: existing PR already addresses it — {reason}"
                if sufficient
                else f"Re-fired alert: committed an incremental fix — {reason}"
            )
        else:
            # First fire: a read-only agent reads the repo (via MCP) and proposes surgical field
            # edits; opsgentic applies them to the real file. No edits -> a proposal PR.
            edits, tool_calls = generate_edits(state)
            pr_url = create_pull_request(
                plan,
                run_id=run_id,
                hypothesis=state.get("hypothesis", ""),
                validation_report=state.get("validation_report"),
                alert=alert,
                edits=edits,
                workload=workload,
            )
            msg = f"Opened remediation PR (edits the manifest; awaiting merge & GitOps sync): {pr_url}"
    except Exception as exc:
        logger.warning("PR creation failed: %s", exc)
        return {
            "execution_status": "failed",
            "messages": [AIMessage(content=f"Failed to open remediation PR: {exc}")],
        }

    return {
        "pr_url": pr_url,
        "execution_status": "applied",
        "remediation_tool_calls": tool_calls,
        "messages": [AIMessage(content=msg)],
    }
