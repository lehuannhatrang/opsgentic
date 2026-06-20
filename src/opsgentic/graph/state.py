from __future__ import annotations

from typing import Annotated, Literal, Optional, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class RemediationPlan(TypedDict, total=False):
    summary: str
    target_repo: str          # owner/repo (or group/subgroup/repo) for display
    repo_url: str
    host: str
    owner: str
    repo: str
    provider: str             # github | gitea | gitlab
    revision: str
    path: str                 # GitOps source path (dir or file)
    file_path: str
    diff: str
    risk: str
    source: str               # labels | argocd | flux


ExecutionStatus = Literal[
    "pending",
    "awaiting_approval",
    "approved",
    "rejected",
    "applied",
    "failed",
]


class MachineState(TypedDict, total=False):
    alert_payload: dict                          # Normalized input (grafana | chat)
    context_data: dict                           # Gathered context (MCP)
    service_ref: dict                            # Resolved workload (namespace/name/kind)
    gitops_target: Optional[dict]                # Resolved repo/path/provider (or None)
    hypothesis: Optional[str]                    # RCA conclusion
    validation_report: Optional[dict]            # Validation Skills output
    remediation_plan: Optional[RemediationPlan]  # Remediation plan
    execution_status: ExecutionStatus
    pr_url: Optional[str]
    rca_attempts: int                            # Self-heal loop counter
    messages: Annotated[list[AnyMessage], add_messages]
