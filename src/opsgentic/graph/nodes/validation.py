from __future__ import annotations

from langchain_core.messages import AIMessage

from opsgentic.config import get_settings
from opsgentic.graph.state import MachineState
from opsgentic.skills.registry import run_validation_skills


def validation_node(state: MachineState) -> dict:
    report = run_validation_skills(state)
    attempts = state.get("rca_attempts", 0)
    target = state.get("gitops_target")
    messages = [AIMessage(content=f"Validation: {report.get('summary')}")]
    update: dict = {"validation_report": report, "messages": messages}

    if not report.get("passed"):
        update["execution_status"] = (
            "failed" if attempts >= get_settings().max_rca_attempts else "pending"
        )
        return update

    if not target:
        update["execution_status"] = "failed"
        messages.append(AIMessage(content="Validation passed but no GitOps repo resolved; escalating."))
        return update

    update["remediation_plan"] = _draft_plan(state, target)
    update["execution_status"] = "awaiting_approval"
    return update


def _draft_plan(state: MachineState, target: dict) -> dict:
    alert = state.get("alert_payload", {})
    return {
        "summary": f"Proposed remediation for {alert.get('title', 'alert')}",
        "target_repo": target["slug"],
        "repo_url": target["repo_url"],
        "host": target["host"],
        "owner": target["owner"],
        "repo": target["repo"],
        "provider": target["provider"],
        "revision": target.get("revision", ""),
        "path": target.get("path", ""),
        "file_path": target.get("path", ""),
        "diff": "# Suggested: raise resources.limits.memory for the affected workload (e.g. 64Mi -> 256Mi).",
        "risk": "medium",
        "source": target.get("source", "unknown"),
    }
