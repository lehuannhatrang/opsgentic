import pytest
from fastapi.testclient import TestClient

from opsgentic import analysis
from opsgentic.main import app

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


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_root_health_probe_responds(client):
    # The plugin health-checks GET / before every analysis.
    assert client.get("/").status_code == 200


def test_analyze_returns_the_plugin_response_shape(client, monkeypatch):
    monkeypatch.setattr(
        analysis, "analyze_sync",
        lambda p: {"promote": False, "confidence": 84, "analysis": "a",
                   "rootCause": "b", "remediation": "c"},
    )
    body = client.post("/a2a/analyze", json=REQUEST).json()
    assert set(body) >= {"promote", "confidence", "analysis", "rootCause", "remediation"}
    assert body["promote"] is False
    assert body["confidence"] == 84


def test_analyze_never_500s_on_internal_failure(client, monkeypatch):
    def boom(_p):
        raise RuntimeError("everything is on fire")

    monkeypatch.setattr(analysis, "analyze_sync", boom)
    resp = client.post("/a2a/analyze", json=REQUEST)
    # A non-200 fails the AnalysisRun outright, so the failure must arrive as a promote.
    assert resp.status_code == 200
    assert resp.json()["promote"] is True
    assert resp.json()["confidence"] == 0


def test_analyze_tolerates_an_empty_body(client, monkeypatch):
    monkeypatch.setattr(analysis, "run_precheck", lambda c: {
        "ran": True, "breached": False, "reason": "within_threshold", "detail": "ok",
        "query": "up", "value": 0.0, "threshold": 0.05,
    })
    resp = client.post("/a2a/analyze", json={})
    assert resp.status_code == 200
    assert resp.json()["promote"] is True
