"""Deterministic canary pre-check.

The Rollouts AI metric plugin calls us on every measurement and has no client-side gate,
so cost control lives here: a single PromQL query answered straight from Prometheus over
HTTP -- no MCP, no LLM -- decides whether the canary is worth waking an agent for.

This is a Validation Skill in the existing sense: plain Python, deterministic, independent
of MCP. When the check cannot run, the default is to escalate rather than to promote
blindly; absent metrics are themselves suspicious.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

import yaml

from opsgentic.config import get_settings

logger = logging.getLogger(__name__)

COMPARISONS = ("gt", "ge", "lt", "le")
MISSING_DATA_MODES = ("escalate", "pass")

_HASH = re.compile(r"rollouts-pod-template-hash=([^,\s]+)")


class PrecheckConfigError(ValueError):
    """Raised when config/canary.yaml is structurally invalid."""


@dataclass(frozen=True)
class PrecheckConfig:
    enabled: bool
    query: str
    threshold: float
    comparison: str
    on_missing_data: str


_DISABLED = PrecheckConfig(
    enabled=False, query="", threshold=0.0, comparison="gt", on_missing_data="escalate"
)


def load_config(path: str | Path | None = None) -> PrecheckConfig:
    """Parse the pre-check config. A missing file disables the pre-check (every analysis
    then reaches the agent), which is safe but expensive -- hence the warning."""
    p = Path(path or get_settings().canary_config_path)
    if not p.exists():
        logger.warning("canary config not found at %s; pre-check disabled", p)
        return _DISABLED
    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as exc:
        raise PrecheckConfigError(f"invalid YAML in {p}: {exc}") from None

    section = raw.get("precheck") or {}
    comparison = str(section.get("comparison", "gt"))
    if comparison not in COMPARISONS:
        raise PrecheckConfigError(
            f"unknown comparison {comparison!r} (known: {', '.join(COMPARISONS)})"
        )
    on_missing = str(section.get("on_missing_data", "escalate"))
    if on_missing not in MISSING_DATA_MODES:
        raise PrecheckConfigError(
            f"unknown on_missing_data {on_missing!r} (known: {', '.join(MISSING_DATA_MODES)})"
        )
    try:
        threshold = float(section.get("threshold", 0.0))
    except (TypeError, ValueError):
        raise PrecheckConfigError("threshold must be a number") from None

    return PrecheckConfig(
        enabled=bool(section.get("enabled", False)),
        query=str(section.get("query") or ""),
        threshold=threshold,
        comparison=comparison,
        on_missing_data=on_missing,
    )


@lru_cache
def load_config_cached() -> PrecheckConfig:
    return load_config()


def _hash_of(selector: str) -> str:
    m = _HASH.search(selector or "")
    return m.group(1) if m else ""


def render_query(query: str, canary: dict) -> str:
    """Substitute placeholders without disturbing PromQL's own braces.

    str.format() is unusable here because PromQL is full of `{}`; explicit token
    replacement is the only safe option.
    """
    values = {
        "{namespace}": canary.get("namespace") or "",
        "{rollout}": canary.get("rollout") or "",
        "{canary_selector}": canary.get("canary_selector") or "",
        "{stable_selector}": canary.get("stable_selector") or "",
        "{canary_hash}": _hash_of(canary.get("canary_selector", "")),
        "{stable_hash}": _hash_of(canary.get("stable_selector", "")),
    }
    for token, value in values.items():
        query = query.replace(token, value)
    return query


def _breached(value: float, threshold: float, comparison: str) -> bool:
    if comparison == "gt":
        return value > threshold
    if comparison == "ge":
        return value >= threshold
    if comparison == "lt":
        return value < threshold
    return value <= threshold


def query_prometheus(query: str) -> Optional[float]:
    """Instant PromQL query against PROMETHEUS_URL. None when the result is empty."""
    import httpx

    base = (get_settings().prometheus_url or "").rstrip("/")
    if not base:
        raise RuntimeError("PROMETHEUS_URL is not configured")

    resp = httpx.get(
        f"{base}/api/v1/query",
        params={"query": query},
        timeout=get_settings().prometheus_timeout_seconds,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("status") != "success":
        raise RuntimeError(f"prometheus returned status {body.get('status')!r}")

    results = ((body.get("data") or {}).get("result")) or []
    if not results:
        return None
    try:
        return float(results[0]["value"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _result(*, ran: bool, breached: bool, reason: str, detail: str,
            query: str = "", value: Optional[float] = None,
            threshold: Optional[float] = None) -> dict:
    return {
        "ran": ran,
        "breached": breached,
        "reason": reason,
        "detail": detail,
        "query": query,
        "value": value,
        "threshold": threshold,
    }


def run_precheck(
    canary: dict,
    config: PrecheckConfig | None = None,
    query_fn: Callable[[str], Optional[float]] | None = None,
) -> dict:
    """Decide whether this canary warrants an agent run.

    `breached=True` means escalate to the agent. Anything that prevents a usable reading
    escalates too, unless `on_missing_data: pass` is configured.
    """
    cfg = config if config is not None else load_config_cached()
    fetch = query_fn or query_prometheus

    if not cfg.enabled:
        return _result(ran=False, breached=True, reason="disabled",
                       detail="pre-check disabled; every analysis reaches the agent")

    query = render_query(cfg.query, canary)
    if not query.strip():
        return _result(ran=False, breached=True, reason="no_query",
                       detail="pre-check enabled but no query configured")

    escalate_on_gap = cfg.on_missing_data == "escalate"

    try:
        value = fetch(query)
    except Exception as exc:
        logger.warning("canary pre-check query failed: %s", exc)
        return _result(ran=False, breached=escalate_on_gap, reason="query_error",
                       detail=str(exc), query=query, threshold=cfg.threshold)

    if value is None:
        return _result(ran=False, breached=escalate_on_gap, reason="no_data",
                       detail="query returned no samples", query=query,
                       threshold=cfg.threshold)

    breach = _breached(value, cfg.threshold, cfg.comparison)
    return _result(
        ran=True,
        breached=breach,
        reason="breach" if breach else "within_threshold",
        # Reads back to an operator in the AnalysisRun, so name the test rather than
        # asserting a relation that may not hold: "0.001 gt 0.05" looks like a false claim.
        detail=(
            f"value {value:g} vs threshold {cfg.threshold:g} ({cfg.comparison}) -> "
            f"{'breach' if breach else 'within threshold'}"
        ),
        query=query,
        value=value,
        threshold=cfg.threshold,
    )
