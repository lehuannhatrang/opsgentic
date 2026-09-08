from langgraph.checkpoint.memory import MemorySaver

from opsgentic.graph.builder import build_app
from opsgentic.pipeline.spec import load_spec_file

SPEC_PATH = "config/pipeline.analysis.yaml"


def spec():
    return load_spec_file(SPEC_PATH)


def test_shipped_analysis_blueprint_is_valid():
    # Mounted as a ConfigMap; a typo here breaks every canary analysis.
    s = spec()
    assert s.entrypoint == "rca"


def test_analysis_blueprint_has_no_human_gate():
    """The plugin blocks on the HTTP call with a 300s timeout — an interrupt would hang
    until it gave up and failed the AnalysisRun."""
    assert spec().interrupt_before == ()


def test_analysis_blueprint_terminates_in_a_verdict():
    ids = {n.id for n in spec().nodes}
    assert "verdict" in ids


def test_analysis_graph_compiles_with_expected_topology():
    g = build_app(MemorySaver(), spec=spec()).get_graph()
    ids = set(g.nodes)
    assert {"rca", "resolve_target", "validation", "verdict", "action"} <= ids

    edges = {(e.source, e.target) for e in g.edges}
    assert ("__start__", "rca") in edges
    assert ("rca", "resolve_target") in edges
    assert ("resolve_target", "validation") in edges
    assert ("validation", "verdict") in edges
    assert ("action", "__end__") in edges


def test_verdict_routes_to_action_or_end():
    g = build_app(MemorySaver(), spec=spec()).get_graph()
    targets = {
        e.target for e in g.edges
        if e.source == "verdict" and getattr(e, "conditional", False)
    }
    assert {"action", "__end__"} <= targets


def test_alert_pipeline_still_gates_before_action():
    """Guard against the analysis blueprint's no-interrupt property leaking into the
    alert pipeline, where the human approval gate is the core safety property."""
    from opsgentic.pipeline.spec import load_spec_file as load

    assert "action" in load("config/pipeline.yaml").interrupt_before
