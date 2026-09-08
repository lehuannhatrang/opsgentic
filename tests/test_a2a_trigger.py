from opsgentic.triggers import a2a

REQUEST = {
    "userId": "argo-rollouts",
    "memoryId": "rollout:demo/payments-api",
    "prompt": "Analyze canary deployment for rollout 'payments-api'. Namespace: demo.",
    "context": {
        "namespace": "demo",
        "rolloutName": "payments-api",
        "stableSelector": "app=payments-api,rollouts-pod-template-hash=aaa",
        "canarySelector": "app=payments-api,rollouts-pod-template-hash=bbb",
    },
}


def test_from_a2a_maps_rollout_to_workload_labels():
    alert = a2a.from_a2a(REQUEST)
    assert alert["source"] == "argo-rollouts"
    labels = alert["labels"]
    assert labels["namespace"] == "demo"
    assert labels["workload"] == "payments-api"
    # The resolver keys off workload_kind; a Rollout is not a Deployment.
    assert labels["workload_kind"] == "Rollout"
    assert alert["raw"] is REQUEST


def test_from_a2a_carries_prompt_as_description():
    alert = a2a.from_a2a(REQUEST)
    assert "payments-api" in alert["description"]


def test_from_a2a_passes_repo_hints_through_as_gitops_labels():
    req = {**REQUEST, "context": {**REQUEST["context"],
                                  "repoUrl": "https://github.com/acme/gitops.git",
                                  "baseBranch": "main"}}
    labels = a2a.from_a2a(req)["labels"]
    # These are the labels resolver._from_labels() already understands, so an explicit
    # repoUrl short-circuits ArgoCD discovery.
    assert labels["gitops_repo"] == "https://github.com/acme/gitops.git"
    assert labels["gitops_revision"] == "main"


def test_from_a2a_tolerates_empty_payload():
    alert = a2a.from_a2a({})
    assert alert["labels"]["namespace"] == "default"
    assert alert["labels"]["workload"] == ""


def test_canary_ref_captures_selectors():
    ref = a2a.canary_ref_from(REQUEST)
    assert ref["namespace"] == "demo"
    assert ref["rollout"] == "payments-api"
    assert ref["canary_selector"].endswith("bbb")
    assert ref["stable_selector"].endswith("aaa")


def test_thread_id_is_stable_and_derived_from_memory_id():
    first = a2a.thread_id_from(REQUEST)
    second = a2a.thread_id_from(dict(REQUEST))
    assert first == second
    # Safe to embed in a URL path such as /runs/{thread_id}.
    assert "/" not in first and ":" not in first
    assert "payments-api" in first


def test_thread_id_falls_back_to_context_without_memory_id():
    req = {k: v for k, v in REQUEST.items() if k != "memoryId"}
    assert a2a.thread_id_from(req) == a2a.thread_id_from(REQUEST)


def test_to_response_maps_verdict_fields():
    verdict = {
        "promote": False,
        "confidence": 91,
        "analysis": "error rate tripled on canary",
        "root_cause": "missing DB index",
        "remediation": "revert image tag",
    }
    body = a2a.to_response(verdict, pr_url="https://github.com/acme/gitops/pull/7")
    assert body["promote"] is False
    assert body["confidence"] == 91
    assert body["rootCause"] == "missing DB index"
    assert body["remediation"] == "revert image tag"
    assert body["prLink"] == "https://github.com/acme/gitops/pull/7"


def test_to_response_omits_pr_link_when_absent():
    body = a2a.to_response({"promote": True, "confidence": 50, "analysis": "ok"})
    assert "prLink" not in body


def test_to_response_clamps_confidence_into_plugin_range():
    # The plugin divides confidence by 100 to produce Measurement.Value.
    assert a2a.to_response({"promote": True, "confidence": 250})["confidence"] == 100
    assert a2a.to_response({"promote": True, "confidence": -5})["confidence"] == 0


def test_to_response_defaults_are_plugin_safe():
    body = a2a.to_response({})
    # Missing verdict must never read as a confident abort.
    assert body["promote"] is True
    assert body["confidence"] == 0
    assert isinstance(body["analysis"], str)
    assert isinstance(body["rootCause"], str)
    assert isinstance(body["remediation"], str)
