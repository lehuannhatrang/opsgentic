from opsgentic.pipeline.registry import ROUTER_REGISTRY, STEP_REGISTRY


def test_step_registry_has_all_default_steps():
    assert {"rca", "resolve_target", "validation", "action"} <= set(STEP_REGISTRY)
    for fn in STEP_REGISTRY.values():
        assert callable(fn)


def test_after_validation_router_matches_legacy_logic():
    route = ROUTER_REGISTRY["after_validation"]
    # passed + plan -> action
    assert route({"validation_report": {"passed": True}, "remediation_plan": {"x": 1}}) == "action"
    # passed but no plan -> self-heal loop back to rca
    assert route({"validation_report": {"passed": True}}) == "rca"
    # exhausted / unresolved -> escalate
    assert route({"execution_status": "failed"}) == "escalate"
    # nothing decided -> loop
    assert route({}) == "rca"
