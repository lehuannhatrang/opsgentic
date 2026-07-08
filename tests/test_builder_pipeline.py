import pytest
from langgraph.checkpoint.memory import MemorySaver

from opsgentic.graph.builder import build_app
from opsgentic.pipeline import spec as specmod


def test_compiled_graph_matches_default_topology():
    g = build_app(MemorySaver()).get_graph()
    ids = set(g.nodes)
    assert {"rca", "resolve_target", "validation", "action"} <= ids
    edges = {(e.source, e.target) for e in g.edges}
    assert ("__start__", "rca") in edges
    assert ("rca", "resolve_target") in edges
    assert ("resolve_target", "validation") in edges
    assert ("action", "__end__") in edges
    cond_targets = {
        e.target for e in g.edges
        if e.source == "validation" and getattr(e, "conditional", False)
    }
    assert {"action", "rca", "__end__"} <= cond_targets


def test_unknown_step_raises():
    raw = {
        "entrypoint": "x",
        "nodes": [{"id": "x", "step": "does_not_exist", "agents": []}],
        "edges": [{"from": "x", "to": "END"}],
        "agents": {},
    }
    s = specmod.parse_spec(raw)
    from opsgentic.pipeline.spec import PipelineSpecError
    with pytest.raises(PipelineSpecError):
        build_app(MemorySaver(), spec=s)


def test_unknown_router_raises():
    raw = {
        "entrypoint": "x",
        "nodes": [{"id": "x", "step": "rca", "agents": []}],
        "edges": [{"from": "x", "route": "ghost_router", "branches": {"go": "END"}}],
        "agents": {},
    }
    s = specmod.parse_spec(raw)
    from opsgentic.pipeline.spec import PipelineSpecError
    with pytest.raises(PipelineSpecError):
        build_app(MemorySaver(), spec=s)
