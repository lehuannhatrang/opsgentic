from __future__ import annotations

import asyncio
import logging
from typing import Optional

from pydantic import BaseModel, Field

from opsgentic.agents.llm import get_llm
from opsgentic.config import get_settings
from opsgentic.mcp.loader import explain_exception, load_connections

logger = logging.getLogger(__name__)

# High-volume tools that would blow the agent's context window if appended verbatim.
_DENY = {"pods_log", "nodes_log", "nodes_stats_summary", "nodes_metrics"}

# The ReAct agent is non-deterministic: a run may yield no edits, or abort on a transient
# MCP tool error. Retry up to this many times (take the first with edits) before falling
# back to a proposal markdown.
_MAX_ATTEMPTS = 6

_SYSTEM = (
    "You are an SRE remediation engineer with READ-ONLY access to a Git repository and a "
    "Kubernetes cluster via tools. Given a root-cause hypothesis and the GitOps source path, "
    "use the read tools to find the manifest file responsible (account for plain manifests, "
    "Kustomize overlays/patches, and Helm values) and decide the MINIMAL field change(s) that "
    "fix it. Read the actual file to confirm the exact repo-relative path and current value. "
    "Change only the field(s) the fix requires; never touch the image, probes, or unrelated fields. "
    "For resource pressure (OOMKilled / high memory or CPU), prefer adjusting "
    "resources.requests/limits rather than the container command or args.\n\n"
    "When done, report the changes as structured edits: each edit is file_path (repo-relative), "
    "yaml_path (a dotted path with optional [name=...] list selectors, e.g. "
    "spec.template.spec.containers[name=payments-api].resources.limits.memory) and the new value "
    "(e.g. '256Mi'). Return ONE edit per distinct field — do not repeat the same field. "
    "Always return at least one edit when the hypothesis warrants a change; never return an empty list."
)


def generate_edits(state: dict) -> Optional[list]:
    """Run a read-only remediation agent that reads the repo (via MCP) and proposes
    surgical field edits. Returns [{path, ops: [{yaml_path, value}]}] or None to fall
    back (MCP disabled, no LLM, no resolved repo, agent error, or no edits proposed)."""
    if not get_settings().mcp_enabled or get_llm() is None:
        return None
    if not state.get("gitops_target"):
        return None
    # Retry: take the first attempt that yields concrete edits. Each attempt is read-only
    # (proposes edits in memory; no commit/PR), so retrying has no side effects.
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            edits = asyncio.run(_run(state))
        except Exception as exc:
            logger.warning("remediation agent attempt %d/%d failed: %s",
                           attempt, _MAX_ATTEMPTS, explain_exception(exc), exc_info=True)
            edits = None
        if edits:
            if attempt > 1:
                logger.info("remediation agent produced edits on attempt %d/%d", attempt, _MAX_ATTEMPTS)
            return edits
        logger.warning("remediation agent attempt %d/%d produced no edits", attempt, _MAX_ATTEMPTS)
    return None


class _FieldEdit(BaseModel):
    file_path: str = Field(description="repo-relative path to the manifest file to change")
    yaml_path: str = Field(
        description="dotted path with optional [name=...] list selectors, e.g. "
        "spec.template.spec.containers[name=payments-api].resources.limits.memory"
    )
    value: str = Field(description="the new value for that field, e.g. '256Mi'")


class _RemediationEdits(BaseModel):
    edits: list[_FieldEdit] = Field(
        default_factory=list,
        description="the minimal set of field changes that fix the issue (empty only if no manifest change is warranted)",
    )


async def _run(state: dict) -> Optional[list]:
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from langgraph.prebuilt import create_react_agent

    connections = load_connections()          # all servers (cluster + repo)
    if not connections:
        return None
    _auth_github(connections)                 # fresh GitHub App token for github-mcp
    tools = [t for t in await MultiServerMCPClient(connections).get_tools() if t.name not in _DENY]

    # response_format: after reading the repo, langgraph forces a final structured answer via
    # with_structured_output(). This is far more reliable than hoping the model voluntarily calls
    # a custom edit tool (qwen3 reads the repo but often ends in prose, yielding no edits).
    # checkpointer=False: do not inherit the parent graph's sync PostgresSaver (no async aget_tuple).
    agent = create_react_agent(
        get_llm(), tools, prompt=_SYSTEM, response_format=_RemediationEdits, checkpointer=False
    )

    target = state.get("gitops_target") or {}
    svc = state.get("service_ref") or {}
    human = (
        f"Repository: {target.get('slug')} (owner={target.get('owner')}, repo={target.get('repo')})\n"
        f"GitOps source path: {target.get('path')}\n"
        f"Branch/revision: {target.get('revision') or 'default'}\n"
        f"Affected workload: {svc.get('kind')} {svc.get('namespace')}/{svc.get('name')}\n\n"
        f"Root cause hypothesis:\n{state.get('hypothesis', '')}\n\n"
        "Read the repository under the GitOps source path, find the manifest responsible, "
        "and return the minimal field edit(s) that fix it."
    )
    result = await agent.ainvoke(
        {"messages": [("user", human)]},
        config={"recursion_limit": get_settings().mcp_recursion_limit},
    )

    structured = result.get("structured_response")
    if structured is None:
        return None
    # Dedup: keep one op per (file, yaml_path) — last value wins. Structured output from
    # some models repeats the same field many times; collapsing avoids a degenerate diff.
    by_file: dict = {}
    for e in structured.edits:
        if e.file_path and e.yaml_path and e.value:
            by_file.setdefault(e.file_path, {})[e.yaml_path] = e.value
    edits = [
        {"path": f, "ops": [{"yaml_path": yp, "value": v} for yp, v in ops.items()]}
        for f, ops in by_file.items()
        if ops
    ]
    return edits or None


def _auth_github(connections: dict) -> None:
    """Authenticate the github MCP server with a fresh GitHub App installation token
    (overrides any static header). github-mcp-server HTTP mode authenticates per-request
    via Authorization: Bearer, and installation tokens are short-lived, so mint one now."""
    conn = connections.get("github")
    if not conn:
        return
    from opsgentic.gitops.github_app import github_app_token

    token = github_app_token()
    if token:
        headers = dict(conn.get("headers") or {})
        headers["Authorization"] = f"Bearer {token}"
        connections["github"] = {**conn, "headers": headers}
