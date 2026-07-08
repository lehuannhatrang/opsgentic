import pytest

from opsgentic.pipeline import spec as specmod


def _minimal_raw():
    return {
        "entrypoint": "a",
        "interrupt_before": ["b"],
        "nodes": [
            {"id": "a", "step": "rca", "agents": ["context"]},
            {"id": "b", "step": "action", "agents": ["remediation"]},
        ],
        "edges": [
            {"from": "a", "route": "after_validation", "branches": {"go": "b", "stop": "END"}},
            {"from": "b", "to": "END"},
        ],
        "agents": {"context": {"tools": ["kubernetes"]}, "remediation": {"tools": []}},
        "off_graph": {"pr-responder": {"tools": ["github"]}},
    }


def test_parse_produces_typed_spec():
    s = specmod.parse_spec(_minimal_raw())
    assert s.entrypoint == "a"
    assert s.interrupt_before == ("b",)
    assert [n.id for n in s.nodes] == ["a", "b"]
    assert s.node_agents == {"a": ["context"], "b": ["remediation"]}
    assert s.agent_tools["context"] == frozenset({"kubernetes"})
    assert s.off_graph_agents == ["pr-responder"]
    cond = [e for e in s.edges if e.conditional]
    assert cond and cond[0].branches == {"go": "b", "stop": "END"}
    straight = [e for e in s.edges if not e.conditional]
    assert straight and straight[0].source == "b" and straight[0].target == "END"


def test_missing_entrypoint_node_rejected():
    raw = _minimal_raw()
    raw["entrypoint"] = "nope"
    with pytest.raises(specmod.PipelineSpecError):
        specmod.parse_spec(raw)


def test_edge_to_unknown_node_rejected():
    raw = _minimal_raw()
    raw["edges"][1]["to"] = "ghost"
    with pytest.raises(specmod.PipelineSpecError):
        specmod.parse_spec(raw)


def test_conditional_branch_to_unknown_node_rejected():
    raw = _minimal_raw()
    raw["edges"][0]["branches"]["go"] = "ghost"
    with pytest.raises(specmod.PipelineSpecError):
        specmod.parse_spec(raw)


def test_node_agent_without_entry_rejected():
    raw = _minimal_raw()
    raw["nodes"][0]["agents"] = ["mystery"]
    with pytest.raises(specmod.PipelineSpecError):
        specmod.parse_spec(raw)


def test_no_nodes_rejected():
    with pytest.raises(specmod.PipelineSpecError):
        specmod.parse_spec({"entrypoint": "a", "nodes": [], "edges": [], "agents": {}})


def test_shipped_default_spec_loads_and_matches_topology():
    specmod.load_spec.cache_clear()
    s = specmod.load_spec()
    assert {n.id for n in s.nodes} == {"rca", "resolve_target", "validation", "action"}
    assert s.entrypoint == "rca"
    assert s.interrupt_before == ("action",)
    assert s.node_agents["rca"] == ["context", "rca"]
    assert s.node_agents["action"] == ["remediation"]
    assert s.agent_tools["context"] == frozenset({"kubernetes", "prometheus"})
    assert s.agent_tools["remediation"] == frozenset({"kubernetes", "github", "prometheus"})
    assert s.off_graph_agents == ["pr-responder"]
    assert s.off_graph_tools["pr-responder"] == frozenset({"kubernetes", "prometheus", "github"})
    # conditional validation edge -> action / rca / END
    cond = [e for e in s.edges if e.conditional and e.source == "validation"]
    assert cond and cond[0].route == "after_validation"
    assert cond[0].branches == {"action": "action", "rca": "rca", "escalate": "END"}
