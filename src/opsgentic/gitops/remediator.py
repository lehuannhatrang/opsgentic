from __future__ import annotations

import asyncio
import logging
from typing import Optional

from opsgentic.agents.llm import get_llm
from opsgentic.config import get_settings
from opsgentic.mcp.loader import explain_exception, load_connections

logger = logging.getLogger(__name__)

# High-volume tools that would blow the agent's context window if appended verbatim.
_DENY = {"pods_log", "nodes_log", "nodes_stats_summary", "nodes_metrics"}

_SYSTEM = (
    "You are an SRE remediation engineer with READ-ONLY access to a Git repository and a "
    "Kubernetes cluster via tools. Given a root-cause hypothesis and the GitOps source path, "
    "find the manifest file responsible (account for plain manifests, Kustomize overlays/"
    "patches, and Helm values) and decide the MINIMAL field change(s) that fix it.\n\n"
    "Do NOT rewrite files or output file contents. Instead, read the actual file to confirm "
    "the exact path and current value, then for EACH field to change call "
    "propose_edit(file_path, yaml_path, value). `yaml_path` is a dotted path with optional "
    "[name=...] list selectors, e.g. "
    "spec.template.spec.containers[name=payments-api].resources.limits.memory with value '256Mi'. "
    "Change only the field(s) the fix requires; never touch the image, probes, or unrelated fields."
)


def generate_edits(state: dict) -> Optional[list]:
    """Run a read-only remediation agent that reads the repo (via MCP) and proposes
    surgical field edits. Returns [{path, ops: [{yaml_path, value}]}] or None to fall
    back (MCP disabled, no LLM, no resolved repo, agent error, or no edits proposed)."""
    if not get_settings().mcp_enabled or get_llm() is None:
        return None
    if not state.get("gitops_target"):
        return None
    try:
        return asyncio.run(_run(state))
    except Exception as exc:
        logger.warning("remediation agent failed: %s", explain_exception(exc))
        return None


async def _run(state: dict) -> Optional[list]:
    from langchain_core.tools import tool
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from langgraph.prebuilt import create_react_agent

    connections = load_connections()          # all servers (cluster + repo)
    if not connections:
        return None
    _auth_github(connections)                 # fresh GitHub App token for github-mcp
    tools = [t for t in await MultiServerMCPClient(connections).get_tools() if t.name not in _DENY]

    edits_by_file: dict = {}

    @tool
    def propose_edit(file_path: str, yaml_path: str, value: str) -> str:
        """Propose setting ONE YAML field. yaml_path uses dots and [name=...] selectors,
        e.g. spec.template.spec.containers[name=payments-api].resources.limits.memory.
        Call once per field. Do NOT send whole files."""
        edits_by_file.setdefault(file_path, []).append({"yaml_path": yaml_path, "value": value})
        return f"recorded {file_path}: {yaml_path}={value}"

    agent = create_react_agent(get_llm(), tools + [propose_edit], prompt=_SYSTEM)

    target = state.get("gitops_target") or {}
    svc = state.get("service_ref") or {}
    human = (
        f"Repository: {target.get('slug')} (owner={target.get('owner')}, repo={target.get('repo')})\n"
        f"GitOps source path: {target.get('path')}\n"
        f"Branch/revision: {target.get('revision') or 'default'}\n"
        f"Affected workload: {svc.get('kind')} {svc.get('namespace')}/{svc.get('name')}\n\n"
        f"Root cause hypothesis:\n{state.get('hypothesis', '')}\n\n"
        "Read the repository under the GitOps source path, find the manifest responsible, "
        "and call propose_edit(file_path, yaml_path, value) for each field to change."
    )
    await agent.ainvoke(
        {"messages": [("user", human)]},
        config={"recursion_limit": get_settings().mcp_recursion_limit},
    )

    edits = [{"path": f, "ops": ops} for f, ops in edits_by_file.items() if ops]
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
