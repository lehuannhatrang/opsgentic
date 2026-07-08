from opsgentic.pipeline.render import render_pipeline
from opsgentic.pipeline.spec import load_spec


def test_render_pipeline_contains_topology():
    load_spec.cache_clear()
    out = render_pipeline(load_spec())
    assert "entrypoint=rca" in out
    assert "interrupt_before=[action]" in out
    for node in ("rca", "resolve_target", "validation", "action"):
        assert node in out
    assert "after_validation" in out
    assert "escalate->END" in out
    assert "kubernetes" in out
    assert "pr-responder" in out


def test_render_pipeline_is_deterministic():
    load_spec.cache_clear()
    spec = load_spec()
    assert render_pipeline(spec) == render_pipeline(spec)
