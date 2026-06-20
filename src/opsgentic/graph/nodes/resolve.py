from __future__ import annotations

from langchain_core.messages import AIMessage

from opsgentic.gitops.resolver import derive_service_ref, resolve_target
from opsgentic.graph.state import MachineState


def resolve_target_node(state: MachineState) -> dict:
    """Map the alerting workload to its GitOps repo/path (labels -> ArgoCD -> Flux)."""
    alert = state.get("alert_payload", {})
    svc = derive_service_ref(alert)
    target = resolve_target(alert)

    if target is None:
        msg = "Could not resolve a GitOps repo for the alert; will escalate."
    else:
        msg = f"Resolved GitOps source ({target.get('source')}): {target.get('slug')} path={target.get('path')}"

    return {"service_ref": svc, "gitops_target": target, "messages": [AIMessage(content=msg)]}
