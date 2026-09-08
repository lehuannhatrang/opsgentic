"""Normalization for the Argo Rollouts AI metric plugin.

`argoproj-labs/rollouts-plugin-metric-ai` posts a canary-analysis request to
`POST /a2a/analyze` and maps the reply onto an AnalysisRun measurement:
`promote: true` -> Successful (Value = confidence/100), `promote: false` -> Failed.
There is no Inconclusive phase, so an uncertain answer must be expressed as
promote-with-low-confidence rather than withheld.
"""

from __future__ import annotations

import re
from typing import Any

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def from_a2a(payload: dict[str, Any]) -> dict:
    """Normalize a plugin request into an alert_payload.

    The rollout is expressed with the same labels the GitOps resolver already reads, so
    an explicit `repoUrl` short-circuits ArgoCD discovery exactly like a Grafana alert
    carrying `gitops_repo`.
    """
    ctx = payload.get("context") or {}
    namespace = ctx.get("namespace") or "default"
    rollout = ctx.get("rolloutName") or ""

    labels = {
        "namespace": namespace,
        "workload": rollout,
        "workload_kind": "Rollout",
    }
    if ctx.get("repoUrl"):
        labels["gitops_repo"] = ctx["repoUrl"]
    if ctx.get("baseBranch"):
        labels["gitops_revision"] = ctx["baseBranch"]

    title = f"Canary analysis for rollout {rollout}" if rollout else "Canary analysis"
    return {
        "source": "argo-rollouts",
        "title": title,
        "description": payload.get("prompt") or title,
        "severity": "unknown",
        "labels": labels,
        "raw": payload,
    }


def canary_ref_from(payload: dict[str, Any]) -> dict:
    """Pod selectors and identity for the canary under analysis."""
    ctx = payload.get("context") or {}
    return {
        "namespace": ctx.get("namespace") or "default",
        "rollout": ctx.get("rolloutName") or "",
        "stable_selector": ctx.get("stableSelector") or "",
        "canary_selector": ctx.get("canarySelector") or "",
        "extra_prompt": ctx.get("extraPrompt") or "",
    }


def thread_id_from(payload: dict[str, Any]) -> str:
    """Stable checkpoint thread for a rollout.

    The plugin sends `memoryId = "rollout:{ns}/{name}"`, identical across every
    AnalysisRun of that rollout, so reusing it as the LangGraph thread_id gives the agent
    memory of earlier analyses for free. Sanitized because the id appears in URL paths
    such as /runs/{thread_id}.
    """
    memory_id = payload.get("memoryId")
    if not memory_id:
        ctx = payload.get("context") or {}
        memory_id = f"rollout:{ctx.get('namespace') or 'default'}/{ctx.get('rolloutName') or ''}"
    return "a2a-" + _UNSAFE.sub("-", str(memory_id))


def to_response(verdict: dict[str, Any] | None, pr_url: str | None = None) -> dict:
    """Map a verdict onto the plugin's response body.

    Defaults are deliberately fail-open: an absent or malformed verdict answers
    promote-with-zero-confidence rather than aborting a live rollout.
    """
    v = verdict or {}
    confidence = v.get("confidence", 0)
    try:
        confidence = int(confidence)
    except (TypeError, ValueError):
        confidence = 0

    body = {
        "promote": bool(v.get("promote", True)),
        "confidence": max(0, min(100, confidence)),
        "analysis": str(v.get("analysis") or ""),
        "rootCause": str(v.get("root_cause") or ""),
        "remediation": str(v.get("remediation") or ""),
    }
    if pr_url:
        body["prLink"] = pr_url
    return body
