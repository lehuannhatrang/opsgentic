from __future__ import annotations

# Single source of truth for which MCP servers each logical agent loads tools from,
# and how the logical agents map onto the LangGraph nodes. The runtime call sites
# (mcp/context.py, conversation/responder.py) and the graph view (graphview.py) both
# import these so the visualized topology can never drift from what actually runs.

# Agent -> set of MCP server names it loads read-only tools from.
AGENT_TOOLS: dict[str, set[str]] = {
    "context": {"kubernetes", "prometheus"},
    "rca": set(),            # reasons over context_data; no direct tools
    "resolver": set(),       # LLM picks from precomputed candidates; no direct tools
    "validation": set(),     # deterministic skill registry; no LLM/MCP
    # Topology only: remediator.py loads all configured servers via load_connections(),
    # so a new MCP server must also be added here to appear under the action node.
    "remediation": {"kubernetes", "github", "prometheus"},
    "pr-responder": {"kubernetes", "prometheus", "github"},
}

# LangGraph node -> the agent(s) that run inside it (order = execution order in the node).
NODE_AGENTS: dict[str, list[str]] = {
    "rca": ["context", "rca"],
    "resolve_target": ["resolver"],
    "validation": ["validation"],
    "action": ["remediation"],
}

# Agents that run outside the alert->remediation DAG (webhook-triggered).
OFF_GRAPH_AGENTS: list[str] = ["pr-responder"]
