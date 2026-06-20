from __future__ import annotations

from typing import Annotated, Literal, Optional, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class RemediationPlan(TypedDict):
    summary: str
    target_repo: str          # Target GitOps repo
    file_path: str            # Manifest to patch
    diff: str                 # Proposed patch
    risk: Literal["low", "medium", "high"]


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
    context_data: dict                           # Gathered context (MCP in M2)
    hypothesis: Optional[str]                    # RCA conclusion
    validation_report: Optional[dict]            # Validation Skills output
    remediation_plan: Optional[RemediationPlan]  # Remediation plan
    execution_status: ExecutionStatus
    pr_url: Optional[str]
    rca_attempts: int                            # Self-heal loop counter
    messages: Annotated[list[AnyMessage], add_messages]
