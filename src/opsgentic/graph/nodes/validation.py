from __future__ import annotations

from langchain_core.messages import AIMessage

from opsgentic.config import get_settings
from opsgentic.graph.state import MachineState
from opsgentic.skills.registry import run_validation_skills


def validation_node(state: MachineState) -> dict:
    report = run_validation_skills(state)
    attempts = state.get("rca_attempts", 0)

    update: dict = {
        "validation_report": report,
        "messages": [AIMessage(content=f"Validation: {report.get('summary')}")],
    }

    if report.get("passed"):
        update["remediation_plan"] = _draft_plan(state)
        update["execution_status"] = "awaiting_approval"
    elif attempts >= get_settings().max_rca_attempts:
        update["execution_status"] = "failed"        # retries exhausted -> escalate
    else:
        update["execution_status"] = "pending"       # loop back to RCA

    return update


def _draft_plan(state: MachineState) -> dict:
    alert = state.get("alert_payload", {})
    labels = alert.get("labels", {}) or {}
    return {
        "summary": f"Proposed remediation for {alert.get('title', 'alert')}",
        "target_repo": labels.get("gitops_repo", "org/gitops-apps"),
        "file_path": "apps/example/deployment.yaml",
        "diff": "# TODO(M3): generated patch from hypothesis",
        "risk": "medium",
    }
