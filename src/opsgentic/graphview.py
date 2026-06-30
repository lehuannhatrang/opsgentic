from __future__ import annotations

from opsgentic import agent_skills
from opsgentic.agent_registry import AGENT_TOOLS, NODE_AGENTS, OFF_GRAPH_AGENTS
from opsgentic.mcp.loader import load_connections

# Pretty labels for the four LangGraph nodes.
_NODE_LABELS = {
    "rca": "RCA Agent",
    "resolve_target": "Resolve Target Agent",
    "validation": "Validation Agent",
    "action": "Action Agent",
}
_ENDPOINT_MAP = {"__end__": "END"}

# Real run triggers that enqueue the graph (runner.enqueue from these entry points).
# Rendered as a "Trigger / Hook" group feeding the first node instead of a bare START.
_TRIGGERS = [
    {"id": "trigger:chat", "label": "Chat API"},
    {"id": "trigger:alert", "label": "Webhook Alert"},
]


def _memory_label() -> str:
    """Reflect the actual checkpoint backend (PostgresSaver when DATABASE_URL is set)."""
    from opsgentic.config import get_settings

    return "Memory (Postgres)" if get_settings().database_url else "Memory (in-mem)"


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

    # Trigger sources (replace the bare START) + END endpoint + flow nodes (where agents run).
    for tr in _TRIGGERS:
        add_node({"id": tr["id"], "type": "trigger", "label": tr["label"], "group": "trigger"})
    add_node({"id": "END", "type": "endpoint", "label": "END", "group": "flow"})
    for node_id in NODE_AGENTS:
        add_node({"id": node_id, "type": "agent", "label": _NODE_LABELS.get(node_id, node_id),
                  "group": "flow", "agents": NODE_AGENTS[node_id]})

    # Memory + servers.
    add_node({"id": "memory", "type": "memory", "label": _memory_label(), "group": "memory"})
    for sname, conn in servers.items():
        add_node({"id": f"server:{sname}", "type": "server", "label": sname, "group": sname,
                  "transport": conn.get("transport"), "url": conn.get("url"), "tools": None})

    # Control-flow edges from the compiled graph. The START edge fans out from every trigger.
    for e in g.edges:
        tgt = _ENDPOINT_MAP.get(e.target, e.target)
        cond = bool(getattr(e, "conditional", False))
        if e.source == "__start__":
            for tr in _TRIGGERS:
                edges.append({"source": tr["id"], "target": tgt, "kind": "flow", "conditional": cond})
            continue
        edges.append({"source": _ENDPOINT_MAP.get(e.source, e.source), "target": tgt,
                      "kind": "flow", "conditional": cond})

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
                add_node({"id": sid, "type": "skill", "label": sk.name, "group": "skill",
                          "description": sk.description, "body": sk.body})
                edges.append({"source": sid, "target": node_id, "kind": "has-skill", "conditional": False})

    for node_id, agents in NODE_AGENTS.items():
        attach(node_id, agents)
        edges.append({"source": node_id, "target": "memory", "kind": "writes-memory", "conditional": False})

    # Off-graph agents (webhook), e.g. pr-responder — tools/skills only, no flow edges.
    _off_labels = {"pr-responder": "PR Responder"}
    for agent in OFF_GRAPH_AGENTS:
        add_node({"id": agent, "type": "agent",
                  "label": _off_labels.get(agent, agent.replace("-", " ").title()),
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
    # Tool-call audit trails, keyed by the flow node that made them: the context agent runs
    # in the rca node; the remediation agent runs in the action node.
    tool_calls: dict = {}
    ctx_calls = (state.get("context_data") or {}).get("tool_calls") or []
    if ctx_calls:
        tool_calls["rca"] = ctx_calls
    action_calls = state.get("remediation_tool_calls") or []
    if action_calls:
        tool_calls["action"] = action_calls
    graph["executed"] = steps
    graph["tool_calls"] = tool_calls
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
