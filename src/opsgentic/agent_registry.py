from __future__ import annotations

# Single source of truth for agent<->node<->tool wiring, DERIVED from the declarative
# pipeline spec (config/pipeline.yaml) so the runtime graph, the tool loaders
# (mcp/context.py, conversation/responder.py) and the console graph view (graphview.py)
# can never drift from the blueprint. The public names below are unchanged so those
# importers keep working without edits.

from opsgentic.pipeline.spec import load_spec

_spec = load_spec()

# Agent -> set of MCP server names it loads read-only tools from (graph agents + off-graph).
AGENT_TOOLS: dict[str, set[str]] = {
    **{agent: set(tools) for agent, tools in _spec.agent_tools.items()},
    **{agent: set(tools) for agent, tools in _spec.off_graph_tools.items()},
}

# LangGraph node -> the agent(s) that run inside it (order = execution order in the node).
NODE_AGENTS: dict[str, list[str]] = _spec.node_agents

# Agents that run outside the alert->remediation DAG (webhook-triggered).
OFF_GRAPH_AGENTS: list[str] = _spec.off_graph_agents
