from opsgentic import graphview


def _nodes_by_type(graph, t):
    return [n for n in graph["nodes"] if n["type"] == t]


def _edge(graph, kind):
    return [e for e in graph["edges"] if e["kind"] == kind]


def test_system_graph_has_flow_nodes_and_endpoints():
    g = graphview.build_system_graph()
    ids = {n["id"] for n in g["nodes"]}
    assert {"rca", "resolve_target", "validation", "action"} <= ids
    assert {"START", "END", "memory"} <= ids
    flow_agents = {n["id"] for n in g["nodes"] if n["type"] == "agent" and n["group"] == "flow"}
    assert {"rca", "resolve_target", "validation", "action"} <= flow_agents


def test_system_graph_conditional_validation_edges():
    g = graphview.build_system_graph()
    flow = _edge(g, "flow")
    cond_targets = {e["target"] for e in flow if e["source"] == "validation" and e["conditional"]}
    assert {"action", "rca", "END"} <= cond_targets
    straight = {(e["source"], e["target"]) for e in flow if not e["conditional"]}
    assert ("START", "rca") in straight
    assert ("rca", "resolve_target") in straight


def test_system_graph_tool_skill_memory_edges():
    g = graphview.build_system_graph()
    uses = {(e["source"], e["target"]) for e in _edge(g, "uses-tool")}
    assert ("action", "server:kubernetes") in uses
    assert ("action", "server:github") in uses
    assert ("action", "server:prometheus") in uses
    has_skill = {(e["source"], e["target"]) for e in _edge(g, "has-skill")}
    assert ("skill:sre", "rca") in has_skill
    writes = {e["source"] for e in _edge(g, "writes-memory")}
    assert {"rca", "resolve_target", "validation", "action"} <= writes


def test_system_graph_pr_responder_is_off_graph():
    g = graphview.build_system_graph()
    pr = [n for n in g["nodes"] if n["id"] == "pr-responder"]
    assert pr and pr[0]["group"] == "webhook"
    flow = _edge(g, "flow")
    assert not any(e["source"] == "pr-responder" or e["target"] == "pr-responder" for e in flow)


def test_servers_present_with_lazy_tools_placeholder():
    g = graphview.build_system_graph()
    servers = {n["id"]: n for n in g["nodes"] if n["type"] == "server"}
    assert "server:kubernetes" in servers
    assert servers["server:kubernetes"]["tools"] is None
