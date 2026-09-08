from __future__ import annotations

from collections.abc import Callable

from opsgentic.config import get_settings
from opsgentic.graph.nodes.action import action_node
from opsgentic.graph.nodes.rca import rca_node
from opsgentic.graph.nodes.resolve import resolve_target_node
from opsgentic.graph.nodes.validation import validation_node
from opsgentic.graph.nodes.verdict import verdict_node
from opsgentic.graph.state import MachineState

# Named step implementations a pipeline node binds to via `step:` in config/pipeline.yaml.
# Adding a genuinely new agent = add its node function here, then reference it from the spec.
STEP_REGISTRY: dict[str, Callable] = {
    "rca": rca_node,
    "resolve_target": resolve_target_node,
    "validation": validation_node,
    "action": action_node,
    "verdict": verdict_node,
}


def _route_after_validation(state: MachineState) -> str:
    report = state.get("validation_report") or {}
    # Route on plan presence (not transient status) so update_state on approve/reject
    # does not re-route this edge and loop back to RCA.
    if report.get("passed") and state.get("remediation_plan"):
        return "action"
    if state.get("execution_status") == "failed":   # retries exhausted or unresolved repo
        return "escalate"
    return "rca"                                     # self-heal loop


def _route_after_verdict(state: MachineState) -> str:
    """Analysis pipeline: a negative verdict may also open a revert PR.

    Opt-in (A2A_REVERT_PR) and only when validation actually drafted a plan to act on —
    entering the action node empty-handed would just fail inside the plugin's deadline.
    """
    verdict = state.get("verdict") or {}
    if verdict.get("promote", True):
        return "done"
    if not get_settings().a2a_revert_pr:
        return "done"
    return "remediate" if state.get("remediation_plan") else "done"


# Named routers a conditional edge binds to via `route:` in config/pipeline.yaml.
ROUTER_REGISTRY: dict[str, Callable] = {
    "after_validation": _route_after_validation,
    "after_verdict": _route_after_verdict,
}
