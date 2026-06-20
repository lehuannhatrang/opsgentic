from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from opsgentic.graph.nodes.action import action_node
from opsgentic.graph.nodes.rca import rca_node
from opsgentic.graph.nodes.resolve import resolve_target_node
from opsgentic.graph.nodes.validation import validation_node
from opsgentic.graph.state import MachineState


def _route_after_validation(state: MachineState) -> str:
    report = state.get("validation_report") or {}
    # Route on plan presence (not transient status) so update_state on approve/reject
    # does not re-route this edge and loop back to RCA.
    if report.get("passed") and state.get("remediation_plan"):
        return "action"
    if state.get("execution_status") == "failed":   # retries exhausted or unresolved repo
        return "escalate"
    return "rca"                                     # self-heal loop


def build_app(checkpointer):
    g = StateGraph(MachineState)
    g.add_node("rca", rca_node)
    g.add_node("resolve_target", resolve_target_node)
    g.add_node("validation", validation_node)
    g.add_node("action", action_node)

    g.add_edge(START, "rca")
    g.add_edge("rca", "resolve_target")
    g.add_edge("resolve_target", "validation")
    g.add_conditional_edges(
        "validation",
        _route_after_validation,
        {"action": "action", "rca": "rca", "escalate": END},
    )
    g.add_edge("action", END)

    # interrupt_before=["action"]: pause for human approval before opening a PR.
    return g.compile(checkpointer=checkpointer, interrupt_before=["action"])
