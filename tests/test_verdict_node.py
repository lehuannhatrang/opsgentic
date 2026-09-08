from opsgentic.graph.nodes import verdict as vmod
from opsgentic.pipeline.registry import ROUTER_REGISTRY


class _FakeStructured:
    def __init__(self, result):
        self._result = result

    def invoke(self, _messages):
        return self._result


class _FakeLLM:
    def __init__(self, result):
        self._result = result
        self.calls = 0

    def with_structured_output(self, _schema):
        self.calls += 1
        return _FakeStructured(self._result)


def _state(**over):
    base = {
        "alert_payload": {"title": "Canary analysis for rollout payments-api"},
        "canary_ref": {"namespace": "demo", "rollout": "payments-api"},
        "hypothesis": "canary pods OOMKill after the new image raised heap usage",
        "validation_report": {"passed": True, "summary": "all checks passed", "results": []},
        "context_data": {"source": "stub"},
        "precheck": {"ran": True, "breached": True, "value": 0.4, "reason": "breach"},
    }
    base.update(over)
    return base


# --- fail-open guarantees -------------------------------------------------------------

def test_no_llm_configured_promotes_with_zero_confidence(monkeypatch):
    monkeypatch.setattr(vmod, "get_llm", lambda: None)
    out = vmod.verdict_node(_state())
    v = out["verdict"]
    assert v["promote"] is True
    assert v["confidence"] == 0
    assert v["source"] == "fallback"


def test_missing_hypothesis_promotes_without_calling_the_llm(monkeypatch):
    llm = _FakeLLM(None)
    monkeypatch.setattr(vmod, "get_llm", lambda: llm)
    out = vmod.verdict_node(_state(hypothesis=None))
    assert out["verdict"]["promote"] is True
    assert out["verdict"]["confidence"] == 0
    assert llm.calls == 0


def test_llm_exception_promotes_rather_than_aborting(monkeypatch):
    class Boom:
        def with_structured_output(self, _schema):
            raise RuntimeError("model unreachable")

    monkeypatch.setattr(vmod, "get_llm", lambda: Boom())
    out = vmod.verdict_node(_state())
    assert out["verdict"]["promote"] is True
    assert out["verdict"]["confidence"] == 0
    assert "model unreachable" in out["verdict"]["analysis"]


# --- agent verdicts -------------------------------------------------------------------

def test_agent_can_vote_to_abort(monkeypatch):
    result = vmod.VerdictAnswer(
        promote=False,
        confidence=88,
        analysis="canary 5xx rate is 8x stable",
        root_cause="new image ships an unbounded cache",
        remediation="revert image tag to v1.4.2",
    )
    monkeypatch.setattr(vmod, "get_llm", lambda: _FakeLLM(result))
    v = vmod.verdict_node(_state())["verdict"]
    assert v["promote"] is False
    assert v["confidence"] == 88
    assert v["root_cause"].startswith("new image")
    assert v["source"] == "agent"


def test_agent_promote_is_passed_through(monkeypatch):
    result = vmod.VerdictAnswer(promote=True, confidence=70, analysis="noise, not regression")
    monkeypatch.setattr(vmod, "get_llm", lambda: _FakeLLM(result))
    v = vmod.verdict_node(_state())["verdict"]
    assert v["promote"] is True
    assert v["confidence"] == 70


def test_confidence_is_clamped(monkeypatch):
    result = vmod.VerdictAnswer(promote=False, confidence=1000, analysis="x")
    monkeypatch.setattr(vmod, "get_llm", lambda: _FakeLLM(result))
    assert vmod.verdict_node(_state())["verdict"]["confidence"] == 100


def test_verdict_node_appends_a_message(monkeypatch):
    monkeypatch.setattr(vmod, "get_llm", lambda: None)
    out = vmod.verdict_node(_state())
    assert out["messages"]


# --- routing --------------------------------------------------------------------------

def test_router_ends_on_promote():
    route = ROUTER_REGISTRY["after_verdict"]
    assert route({"verdict": {"promote": True}}) == "done"


def test_router_remediates_on_abort_when_enabled(monkeypatch):
    from opsgentic import config

    monkeypatch.setenv("A2A_REVERT_PR", "true")
    config.get_settings.cache_clear()
    try:
        route = ROUTER_REGISTRY["after_verdict"]
        state = {"verdict": {"promote": False}, "remediation_plan": {"summary": "x"}}
        assert route(state) == "remediate"
    finally:
        config.get_settings.cache_clear()


def test_router_ends_on_abort_when_revert_disabled(monkeypatch):
    from opsgentic import config

    monkeypatch.setenv("A2A_REVERT_PR", "false")
    config.get_settings.cache_clear()
    try:
        route = ROUTER_REGISTRY["after_verdict"]
        assert route({"verdict": {"promote": False}, "remediation_plan": {"s": 1}}) == "done"
    finally:
        config.get_settings.cache_clear()


def test_router_ends_when_no_plan_to_act_on(monkeypatch):
    from opsgentic import config

    monkeypatch.setenv("A2A_REVERT_PR", "true")
    config.get_settings.cache_clear()
    try:
        route = ROUTER_REGISTRY["after_verdict"]
        # Nothing to open a PR from -> do not enter the action node.
        assert route({"verdict": {"promote": False}}) == "done"
    finally:
        config.get_settings.cache_clear()
