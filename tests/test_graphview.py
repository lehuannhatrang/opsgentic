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


class _Snap:
    """Minimal stand-in for a LangGraph StateSnapshot (only .metadata is read)."""
    def __init__(self, metadata):
        self.metadata = metadata


def test_executed_steps_orders_nodes_and_counts_loops():
    # oldest-first history: rca -> resolve -> validation -> rca (loop) -> resolve -> validation
    history = [
        _Snap({"source": "input", "writes": None}),
        _Snap({"source": "loop", "writes": {"rca": {}}}),
        _Snap({"source": "loop", "writes": {"resolve_target": {}}}),
        _Snap({"source": "loop", "writes": {"validation": {}}}),
        _Snap({"source": "loop", "writes": {"rca": {}}}),
        _Snap({"source": "loop", "writes": {"resolve_target": {}}}),
        _Snap({"source": "loop", "writes": {"validation": {}}}),
    ]
    steps = graphview.executed_steps(history)
    assert [s["node"] for s in steps] == [
        "rca", "resolve_target", "validation", "rca", "resolve_target", "validation",
    ]
    assert [s["step"] for s in steps] == [0, 1, 2, 3, 4, 5]
    rca_iters = [s["iteration"] for s in steps if s["node"] == "rca"]
    assert rca_iters == [1, 2]


def test_executed_steps_ignores_non_node_writes():
    history = [
        _Snap({"source": "update", "writes": {"execution_status": "approved"}}),
        _Snap({"source": "loop", "writes": {"action": {}}}),
    ]
    steps = graphview.executed_steps(history)
    assert [s["node"] for s in steps] == ["action"]


def test_list_server_tools_unknown_server_returns_empty():
    out = graphview.list_server_tools("does-not-exist")
    assert out["server"] == "does-not-exist"
    assert out["tools"] == []
    assert out["error"]


class _FakeApp:
    def __init__(self, history):
        self._history = history

    def get_state_history(self, _cfg):
        return list(self._history)


def test_build_run_graph_awaiting_approval_overlay(monkeypatch):
    from opsgentic import runner

    # newest-first history (build_run_graph reverses it): rca -> resolve -> validation ran.
    history = [
        _Snap({"source": "loop", "writes": {"validation": {}}}),
        _Snap({"source": "loop", "writes": {"resolve_target": {}}}),
        _Snap({"source": "loop", "writes": {"rca": {}}}),
        _Snap({"source": "input", "writes": None}),
    ]
    snap = {
        "thread_id": "t1",
        "status": "awaiting_approval",
        "next": ["action"],
        "summary": {},
        "state": {
            "hypothesis": "memory limit too low",
            "context_data": {"tool_calls": [{"server": "kubernetes", "name": "pods_list"}]},
            "remediation_plan": {"summary": "bump limit"},
        },
    }
    monkeypatch.setattr(runner, "get_run", lambda tid: snap)
    monkeypatch.setattr(runner, "_config", lambda tid: {})
    monkeypatch.setattr(runner, "_app", _FakeApp(history))

    g = graphview.build_run_graph("t1")
    status = {n["id"]: n.get("status") for n in g["nodes"] if n.get("group") == "flow" and n["type"] == "agent"}
    assert status["rca"] == "ran"
    assert status["resolve_target"] == "ran"
    assert status["validation"] == "ran"
    assert status["action"] == "current"          # interrupt_before -> next node
    assert [s["node"] for s in g["executed"]] == ["rca", "resolve_target", "validation"]
    assert g["tool_calls"]["rca"]                  # context tool calls surfaced on rca
    assert g["run"]["hypothesis"] == "memory limit too low"


def test_build_run_graph_pr_comment_path(monkeypatch):
    from opsgentic import runner

    snap = {
        "thread_id": "prcomment-42",
        "status": "completed",
        "summary": {"source": "github-comment", "kind": "answer",
                    "reply": "here is the evidence", "comment": "why?",
                    "tool_calls": [{"server": "github", "name": "get_file_contents"}]},
    }
    monkeypatch.setattr(runner, "get_run", lambda tid: snap)

    g = graphview.build_run_graph("prcomment-42")
    ids = {n["id"] for n in g["nodes"]}
    assert "pr-responder" in ids
    assert "START" not in ids                      # comment runs have no DAG
    assert g["executed"] == [{"step": 0, "node": "pr-responder", "iteration": 1}]
    assert g["tool_calls"]["pr-responder"]
    assert g["run"]["kind"] == "answer"
