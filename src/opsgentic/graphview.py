from __future__ import annotations

from opsgentic import agent_skills
from opsgentic.agent_registry import AGENT_TOOLS, NODE_AGENTS, OFF_GRAPH_AGENTS
from opsgentic.mcp.loader import load_connections

# Pretty labels for the four LangGraph nodes.
_NODE_LABELS = {
    "rca": "RCA",
    "resolve_target": "Resolve target",
    "validation": "Validation",
    "action": "Action (remediation)",
}
_ENDPOINT_MAP = {"__start__": "START", "__end__": "END"}


def _compiled_graph():
    """Topology only — the checkpointer choice does not affect get_graph()."""
    from langgraph.checkpoint.memory import MemorySaver

    from opsgentic.graph.builder import build_app

    return build_app(MemorySaver()).get_graph()


def _servers() -> dict:
    return load_connections()


def build_system_graph() -> dict:
    """Static topology: flow nodes (agents), MCP servers, skills, memory, and the
    control-flow / uses-tool / has-skill / writes-memory edges between them."""
    g = _compiled_graph()
    servers = _servers()
    nodes: list[dict] = []
    edges: list[dict] = []
    node_ids: set[str] = set()

    def add_node(node: dict) -> None:
        if node["id"] not in node_ids:
            nodes.append(node)
            node_ids.add(node["id"])

    # Endpoints + flow nodes (each flow node is where an agent runs).
    add_node({"id": "START", "type": "endpoint", "label": "START", "group": "flow"})
    add_node({"id": "END", "type": "endpoint", "label": "END", "group": "flow"})
    for node_id in NODE_AGENTS:
        add_node({"id": node_id, "type": "agent", "label": _NODE_LABELS.get(node_id, node_id),
                  "group": "flow", "agents": NODE_AGENTS[node_id]})

    # Memory + servers.
    add_node({"id": "memory", "type": "memory", "label": "Memory (checkpoint)", "group": "memory"})
    for sname, conn in servers.items():
        add_node({"id": f"server:{sname}", "type": "server", "label": sname, "group": sname,
                  "transport": conn.get("transport"), "url": conn.get("url"), "tools": None})

    # Control-flow edges from the compiled graph.
    for e in g.edges:
        src = _ENDPOINT_MAP.get(e.source, e.source)
        tgt = _ENDPOINT_MAP.get(e.target, e.target)
        edges.append({"source": src, "target": tgt, "kind": "flow",
                      "conditional": bool(getattr(e, "conditional", False))})

    skills = agent_skills._load_all()

    def attach(node_id: str, agents: list[str]) -> None:
        servers_for: set[str] = set().union(*(AGENT_TOOLS.get(a, set()) for a in agents)) if agents else set()
        for s in sorted(servers_for):
            if f"server:{s}" in node_ids:
                edges.append({"source": node_id, "target": f"server:{s}",
                              "kind": "uses-tool", "conditional": False})
        for sk in skills:
            if any(a in sk.agents for a in agents):
                sid = f"skill:{sk.name}"
                add_node({"id": sid, "type": "skill", "label": sk.name, "group": "skill"})
                edges.append({"source": sid, "target": node_id, "kind": "has-skill", "conditional": False})

    for node_id, agents in NODE_AGENTS.items():
        attach(node_id, agents)
        edges.append({"source": node_id, "target": "memory", "kind": "writes-memory", "conditional": False})

    # Off-graph agents (webhook), e.g. pr-responder — tools/skills only, no flow edges.
    for agent in OFF_GRAPH_AGENTS:
        add_node({"id": agent, "type": "agent", "label": agent.replace("-", " ").title(),
                  "group": "webhook", "agents": [agent]})
        attach(agent, [agent])

    return {"nodes": nodes, "edges": edges}
