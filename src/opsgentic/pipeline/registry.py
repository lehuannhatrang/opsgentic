from __future__ import annotations

from collections.abc import Callable

from opsgentic.graph.nodes.action import action_node
from opsgentic.graph.nodes.rca import rca_node
from opsgentic.graph.nodes.resolve import resolve_target_node
from opsgentic.graph.nodes.validation import validation_node
from opsgentic.graph.state import MachineState

# Named step implementations a pipeline node binds to via `step:` in config/pipeline.yaml.
# Adding a genuinely new agent = add its node function here, then reference it from the spec.
STEP_REGISTRY: dict[str, Callable] = {
    "rca": rca_node,
    "resolve_target": resolve_target_node,
    "validation": validation_node,
    "action": action_node,
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


# Named routers a conditional edge binds to via `route:` in config/pipeline.yaml.
ROUTER_REGISTRY: dict[str, Callable] = {
    "after_validation": _route_after_validation,
}
