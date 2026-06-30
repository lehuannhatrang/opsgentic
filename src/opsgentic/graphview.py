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


_FLOW_NODES = ("rca", "resolve_target", "validation", "action")


def executed_steps(history) -> list[dict]:
    """Reconstruct the ordered list of executed graph nodes from a state history
    (oldest-first iterable of objects exposing `.metadata`). Each loop write that
    names a flow node is one executed step; repeats get an incrementing iteration."""
    steps: list[dict] = []
    counts: dict[str, int] = {}
    for snap in history:
        md = getattr(snap, "metadata", None) or {}
        if md.get("source") != "loop":
            continue
        for node in (md.get("writes") or {}):
            if node in _FLOW_NODES:
                counts[node] = counts.get(node, 0) + 1
                steps.append({"step": len(steps), "node": node, "iteration": counts[node]})
    return steps


def _comment_run_graph(snap: dict) -> dict:
    """Minimal pr-responder trace for a PR-comment run (no graph checkpoint)."""
    s = snap.get("summary") or {}
    servers = _servers()
    nodes = [{"id": "pr-responder", "type": "agent", "label": "PR responder",
              "group": "webhook", "status": "ran"}]
    edges = []
    for srv in sorted(AGENT_TOOLS["pr-responder"]):
        if srv in servers:
            nodes.append({"id": f"server:{srv}", "type": "server", "label": srv,
                          "group": srv, "tools": None})
            edges.append({"source": "pr-responder", "target": f"server:{srv}",
                          "kind": "uses-tool", "conditional": False})
    return {
        "nodes": nodes, "edges": edges,
        "executed": [{"step": 0, "node": "pr-responder", "iteration": 1}],
        "tool_calls": {"pr-responder": s.get("tool_calls") or []},
        "run": {"thread_id": snap.get("thread_id"), "status": snap.get("status"),
                "kind": s.get("kind"), "reply": s.get("reply"), "comment": s.get("comment")},
    }


def build_run_graph(thread_id: str) -> dict:
    """System topology overlaid with one run's actual execution trace."""
    from opsgentic import runner

    snap = runner.get_run(thread_id)
    if (snap.get("summary") or {}).get("source") == "github-comment" or thread_id.startswith("prcomment-"):
        return _comment_run_graph(snap)

    history = list(runner._app.get_state_history(runner._config(thread_id)))
    history.reverse()  # get_state_history yields newest-first
    steps = executed_steps(history)

    graph = build_system_graph()
    ran = {s["node"] for s in steps}
    nxt = set(snap.get("next") or [])
    for n in graph["nodes"]:
        if n.get("group") == "flow" and n["type"] == "agent":
            n["status"] = "current" if n["id"] in nxt else ("ran" if n["id"] in ran else "pending")

    state = snap.get("state") or {}
    # Only the context phase (rca node) records a tool-call audit trail in state today;
    # the action node's remediator calls are not persisted, so the action node shows none.
    tool_calls = (state.get("context_data") or {}).get("tool_calls") or []
    graph["executed"] = steps
    graph["tool_calls"] = {"rca": tool_calls} if tool_calls else {}
    graph["run"] = {
        "thread_id": thread_id,
        "status": snap.get("status"),
        "hypothesis": state.get("hypothesis"),
        "remediation_plan": state.get("remediation_plan"),
        "pr_url": state.get("pr_url"),
        "execution_status": state.get("execution_status"),
        "error": snap.get("error"),
    }
    return graph


def list_server_tools(server: str) -> dict:
    """Connect to one configured MCP server and return its real tool names. Returns
    an empty list + error string (never raises) so the UI degrades gracefully when a
    server is not deployed."""
    import asyncio

    from opsgentic.mcp.loader import explain_exception

    conns = load_connections(include={server})
    if server not in conns:
        return {"server": server, "tools": [], "error": "not configured"}
    try:
        from opsgentic.mcp.diagnose import _probe

        tools = asyncio.run(_probe(server, conns[server]))
        return {"server": server, "tools": sorted(tools), "error": None}
    except Exception as exc:
        return {"server": server, "tools": [], "error": explain_exception(exc)}
