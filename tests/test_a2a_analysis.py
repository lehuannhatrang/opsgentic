import asyncio

import pytest

from opsgentic import analysis

REQUEST = {
    "userId": "argo-rollouts",
    "memoryId": "rollout:demo/payments-api",
    "prompt": "Analyze canary deployment for rollout 'payments-api'.",
    "context": {
        "namespace": "demo",
        "rolloutName": "payments-api",
        "stableSelector": "app=payments-api,rollouts-pod-template-hash=aaa",
        "canarySelector": "app=payments-api,rollouts-pod-template-hash=bbb",
    },
}


def _precheck(breached, ran=True, reason="breach", value=0.4):
    return {
        "ran": ran, "breached": breached, "reason": reason,
        "detail": "stub", "query": "up", "value": value, "threshold": 0.05,
    }


# --- pre-check short-circuit ----------------------------------------------------------

def test_precheck_within_threshold_skips_the_graph(monkeypatch):
    called = []
    monkeypatch.setattr(analysis, "run_precheck", lambda c: _precheck(False, reason="within_threshold"))
    monkeypatch.setattr(analysis, "_run_graph", lambda *a, **k: called.append(1))

    body = analysis.analyze_sync(REQUEST)
    assert body["promote"] is True
    assert body["confidence"] > 0        # a real reading that passed is genuine evidence
    assert not called                    # the agent was never woken


def test_precheck_pass_without_data_promotes_with_no_confidence(monkeypatch):
    monkeypatch.setattr(
        analysis, "run_precheck", lambda c: _precheck(False, ran=False, reason="no_data", value=None)
    )
    monkeypatch.setattr(analysis, "_run_graph", lambda *a, **k: pytest.fail("should not run"))

    body = analysis.analyze_sync(REQUEST)
    assert body["promote"] is True
    assert body["confidence"] == 0       # no evidence at all -> claim no confidence


def test_breach_runs_the_graph_and_returns_its_verdict(monkeypatch):
    monkeypatch.setattr(analysis, "run_precheck", lambda c: _precheck(True))
    monkeypatch.setattr(
        analysis, "_run_graph",
        lambda *a, **k: ({"promote": False, "confidence": 90, "analysis": "bad",
                          "root_cause": "rc", "remediation": "revert", "source": "agent"}, None),
    )
    body = analysis.analyze_sync(REQUEST)
    assert body["promote"] is False
    assert body["confidence"] == 90
    assert body["rootCause"] == "rc"


def test_pr_link_is_surfaced_when_the_graph_opened_one(monkeypatch):
    monkeypatch.setattr(analysis, "run_precheck", lambda c: _precheck(True))
    monkeypatch.setattr(
        analysis, "_run_graph",
        lambda *a, **k: ({"promote": False, "confidence": 80, "analysis": "x"},
                         "https://github.com/acme/gitops/pull/9"),
    )
    assert analysis.analyze_sync(REQUEST)["prLink"].endswith("/pull/9")


# --- fail-open ------------------------------------------------------------------------

def test_graph_exception_promotes_rather_than_aborting(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("checkpointer exploded")

    monkeypatch.setattr(analysis, "run_precheck", lambda c: _precheck(True))
    monkeypatch.setattr(analysis, "_run_graph", boom)

    body = analysis.analyze_sync(REQUEST)
    assert body["promote"] is True
    assert body["confidence"] == 0
    assert "checkpointer exploded" in body["analysis"]


def test_precheck_exception_still_reaches_the_graph(monkeypatch):
    def boom(_c):
        raise RuntimeError("prom down")

    monkeypatch.setattr(analysis, "run_precheck", boom)
    monkeypatch.setattr(
        analysis, "_run_graph",
        lambda *a, **k: ({"promote": True, "confidence": 40, "analysis": "ok"}, None),
    )
    # A broken pre-check must not silently promote; it escalates to the agent.
    assert analysis.analyze_sync(REQUEST)["confidence"] == 40


def test_deadline_exceeded_promotes_with_zero_confidence(monkeypatch):
    from opsgentic import config

    monkeypatch.setenv("A2A_DEADLINE_SECONDS", "0.05")
    config.get_settings.cache_clear()

    def slow(_payload):
        import time
        time.sleep(1.0)
        return {"promote": False, "confidence": 99}

    monkeypatch.setattr(analysis, "analyze_sync", slow)
    try:
        body = asyncio.run(analysis.analyze(REQUEST))
        assert body["promote"] is True
        assert body["confidence"] == 0
        assert "deadline" in body["analysis"].lower()
    finally:
        config.get_settings.cache_clear()


def test_analyze_returns_sync_result_within_deadline(monkeypatch):
    monkeypatch.setattr(analysis, "analyze_sync", lambda p: {"promote": False, "confidence": 77})
    body = asyncio.run(analysis.analyze(REQUEST))
    assert body["confidence"] == 77


# --- graph invocation details ---------------------------------------------------------

def test_run_graph_resets_per_run_state_but_keeps_the_thread(monkeypatch):
    """The thread_id is stable per rollout so the agent remembers earlier analyses, which
    means stale per-run fields would otherwise leak into the next verdict."""
    seen = {}

    class FakeApp:
        def invoke(self, initial, config):
            seen["initial"] = initial
            seen["config"] = config

        def get_state(self, _config):
            class S:
                values = {"verdict": {"promote": True, "confidence": 5}, "pr_url": None}
                next = ()
            return S()

    monkeypatch.setattr(analysis, "_get_app", lambda: FakeApp())
    analysis._run_graph(REQUEST, _precheck(True))

    initial = seen["initial"]
    assert initial["verdict"] is None
    assert initial["hypothesis"] is None
    assert initial["validation_report"] is None
    assert initial["remediation_plan"] is None
    assert initial["pr_url"] is None
    assert initial["rca_attempts"] == 0
    assert initial["canary_ref"]["rollout"] == "payments-api"
    assert initial["precheck"]["breached"] is True
    assert seen["config"]["configurable"]["thread_id"] == "a2a-rollout-demo-payments-api"
