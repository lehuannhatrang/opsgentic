import pytest

from opsgentic.skills import canary_precheck as cp

CANARY = {
    "namespace": "demo",
    "rollout": "payments-api",
    "canary_selector": "app=payments-api,rollouts-pod-template-hash=bbb111",
    "stable_selector": "app=payments-api,rollouts-pod-template-hash=aaa000",
}


def cfg(**over):
    base = dict(
        enabled=True,
        query='rate(errors{namespace="{namespace}",pod=~".*{canary_hash}.*"}[2m])',
        threshold=0.05,
        comparison="gt",
        on_missing_data="escalate",
    )
    base.update(over)
    return cp.PrecheckConfig(**base)


def test_disabled_config_always_escalates():
    out = cp.run_precheck(CANARY, cfg(enabled=False), query_fn=lambda q: 0.0)
    assert out["breached"] is True
    assert out["ran"] is False
    assert out["reason"] == "disabled"


def test_value_within_threshold_does_not_escalate():
    out = cp.run_precheck(CANARY, cfg(), query_fn=lambda q: 0.01)
    assert out["breached"] is False
    assert out["ran"] is True
    assert out["value"] == 0.01


def test_value_over_threshold_escalates():
    out = cp.run_precheck(CANARY, cfg(), query_fn=lambda q: 0.42)
    assert out["breached"] is True
    assert out["ran"] is True
    assert out["reason"] == "breach"


def test_placeholders_are_substituted_from_canary_ref():
    seen = {}

    def spy(q):
        seen["q"] = q
        return 0.0

    cp.run_precheck(CANARY, cfg(), query_fn=spy)
    assert 'namespace="demo"' in seen["q"]
    # The pod-template-hash is what actually distinguishes canary pods from stable ones.
    assert "bbb111" in seen["q"]
    assert "{namespace}" not in seen["q"]
    assert "{canary_hash}" not in seen["q"]
    # PromQL's own braces must survive substitution untouched.
    assert seen["q"].startswith("rate(errors{")


def test_rollout_and_selector_placeholders_available():
    seen = {}

    def spy(q):
        seen["q"] = q
        return 0.0

    c = cfg(query='up{rollout="{rollout}",sel="{canary_selector}",sh="{stable_hash}"}')
    cp.run_precheck(CANARY, c, query_fn=spy)
    assert 'rollout="payments-api"' in seen["q"]
    assert 'sh="aaa000"' in seen["q"]
    assert "app=payments-api" in seen["q"]


@pytest.mark.parametrize(
    "comparison,value,threshold,expected",
    [
        ("gt", 0.06, 0.05, True),
        ("gt", 0.05, 0.05, False),
        ("ge", 0.05, 0.05, True),
        ("lt", 0.90, 0.99, True),
        ("lt", 0.99, 0.99, False),
        ("le", 0.99, 0.99, True),
    ],
)
def test_comparison_operators(comparison, value, threshold, expected):
    out = cp.run_precheck(
        CANARY, cfg(comparison=comparison, threshold=threshold), query_fn=lambda q: value
    )
    assert out["breached"] is expected


def test_missing_data_escalates_by_default():
    out = cp.run_precheck(CANARY, cfg(), query_fn=lambda q: None)
    assert out["breached"] is True
    assert out["ran"] is False
    assert out["reason"] == "no_data"


def test_missing_data_can_be_configured_to_pass():
    out = cp.run_precheck(CANARY, cfg(on_missing_data="pass"), query_fn=lambda q: None)
    assert out["breached"] is False
    assert out["reason"] == "no_data"


def test_query_error_escalates_rather_than_promoting_blindly():
    def boom(q):
        raise RuntimeError("prometheus unreachable")

    out = cp.run_precheck(CANARY, cfg(), query_fn=boom)
    assert out["breached"] is True
    assert out["ran"] is False
    assert out["reason"] == "query_error"
    assert "prometheus unreachable" in out["detail"]


def test_query_error_respects_pass_mode():
    def boom(q):
        raise RuntimeError("down")

    out = cp.run_precheck(CANARY, cfg(on_missing_data="pass"), query_fn=boom)
    assert out["breached"] is False


def test_blank_query_escalates():
    out = cp.run_precheck(CANARY, cfg(query="   "), query_fn=lambda q: 0.0)
    assert out["breached"] is True
    assert out["reason"] == "no_query"


# --- config loading -------------------------------------------------------------------

def test_load_config_reads_yaml(tmp_path):
    p = tmp_path / "canary.yaml"
    p.write_text(
        "version: '1'\n"
        "precheck:\n"
        "  enabled: true\n"
        "  query: up\n"
        "  threshold: 0.2\n"
        "  comparison: ge\n"
        "  on_missing_data: pass\n"
    )
    c = cp.load_config(p)
    assert c.enabled is True
    assert c.query == "up"
    assert c.threshold == 0.2
    assert c.comparison == "ge"
    assert c.on_missing_data == "pass"


def test_load_config_missing_file_disables_precheck(tmp_path):
    c = cp.load_config(tmp_path / "nope.yaml")
    assert c.enabled is False


def test_load_config_rejects_unknown_comparison(tmp_path):
    p = tmp_path / "canary.yaml"
    p.write_text("precheck:\n  enabled: true\n  query: up\n  comparison: sideways\n")
    with pytest.raises(cp.PrecheckConfigError):
        cp.load_config(p)


def test_load_config_rejects_unknown_missing_data_mode(tmp_path):
    p = tmp_path / "canary.yaml"
    p.write_text("precheck:\n  enabled: true\n  query: up\n  on_missing_data: shrug\n")
    with pytest.raises(cp.PrecheckConfigError):
        cp.load_config(p)


def test_shipped_default_config_is_valid():
    # config/canary.yaml is mounted as a ConfigMap; a typo there breaks every analysis.
    c = cp.load_config("config/canary.yaml")
    assert c.query.strip()
    assert c.comparison in cp.COMPARISONS
    assert c.on_missing_data in cp.MISSING_DATA_MODES
