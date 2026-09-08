"""Argo Rollouts canary analysis.

Serves `POST /a2a/analyze` for argoproj-labs/rollouts-plugin-metric-ai. Three properties of
that plugin shape everything here:

* it blocks on the call with a 300s client timeout, so we answer synchronously under a
  tighter deadline;
* it understands only promote/abort — there is no Inconclusive phase — so every uncertain
  path answers promote-with-zero-confidence rather than aborting a live rollout;
* it has no client-side gate, so the deterministic pre-check that keeps LLM cost bounded
  runs here, first.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from opsgentic.config import get_settings
from opsgentic.skills.canary_precheck import run_precheck
from opsgentic.triggers import a2a

logger = logging.getLogger(__name__)

# Confidence attached to a promote that the deterministic pre-check decided on its own.
# A real reading inside the threshold is genuine evidence, but weaker than an agent that
# looked at the cluster -- so: confident enough to be useful, not maximal.
_PRECHECK_CONFIDENCE = 75

_app = None
# Bound to the loop that created it: asyncio primitives cannot be shared across event
# loops, so the loop is part of the cache key rather than assuming a single global loop.
_semaphore: Optional[tuple[asyncio.AbstractEventLoop, asyncio.Semaphore]] = None


def _get_app():
    """Compile the analysis graph once, sharing the alert path's checkpointer so a
    rollout's analysis history lands in the same store."""
    global _app
    if _app is None:
        from opsgentic import runner
        from opsgentic.graph.builder import build_app
        from opsgentic.pipeline.spec import load_spec_file

        spec = load_spec_file(get_settings().analysis_pipeline_config_path)
        _app = build_app(runner._checkpointer, spec=spec)
        logger.info("analysis pipeline compiled from %s",
                    get_settings().analysis_pipeline_config_path)
    return _app


def _run_graph(payload: dict, precheck: dict) -> tuple[dict, Optional[str]]:
    """Run the analysis graph for one canary. Returns (verdict, pr_url)."""
    app = _get_app()
    thread_id = a2a.thread_id_from(payload)
    config = {"configurable": {"thread_id": thread_id}}

    # The thread is stable per rollout so the agent can see earlier analyses, which also
    # means last run's conclusions are still in the channels. Reset everything derived
    # per-run; only `messages` should carry across.
    initial = {
        "alert_payload": a2a.from_a2a(payload),
        "canary_ref": a2a.canary_ref_from(payload),
        "precheck": precheck,
        "execution_status": "pending",
        "hypothesis": None,
        "validation_report": None,
        "remediation_plan": None,
        "gitops_target": None,
        "verdict": None,
        "pr_url": None,
        "rca_attempts": 0,
    }

    app.invoke(initial, config)
    values = dict(app.get_state(config).values)
    return values.get("verdict") or {}, values.get("pr_url")


def _precheck_verdict(precheck: dict) -> dict:
    """Promote decided by the deterministic gate alone, without waking the agent."""
    ran = bool(precheck.get("ran"))
    detail = precheck.get("detail") or ""
    if ran:
        analysis_text = f"Canary within threshold ({detail}); promoted without agent analysis."
    else:
        # on_missing_data: pass — nothing was actually measured, so claim nothing.
        analysis_text = (
            f"Pre-check could not evaluate the canary ({precheck.get('reason')}: {detail}) "
            "and is configured to pass; promoted with no confidence."
        )
    return {
        "promote": True,
        "confidence": _PRECHECK_CONFIDENCE if ran else 0,
        "analysis": analysis_text,
        "root_cause": "",
        "remediation": "",
        "source": "precheck",
    }


def analyze_sync(payload: dict) -> dict:
    """Pre-check, then the analysis graph on breach. Returns the plugin response body."""
    canary = a2a.canary_ref_from(payload)

    try:
        precheck = run_precheck(canary)
    except Exception as exc:
        # A broken gate must not promote blindly — escalate to the agent instead.
        logger.warning("canary pre-check raised: %s", exc)
        precheck = {"ran": False, "breached": True, "reason": "precheck_error",
                    "detail": str(exc), "query": "", "value": None, "threshold": None}

    if not precheck.get("breached"):
        logger.info("canary pre-check passed for %s/%s; skipping agent analysis",
                    canary.get("namespace"), canary.get("rollout"))
        return a2a.to_response(_precheck_verdict(precheck))

    try:
        verdict, pr_url = _run_graph(payload, precheck)
    except Exception as exc:
        logger.exception("canary analysis failed")
        return a2a.to_response({
            "promote": True,
            "confidence": 0,
            "analysis": f"Analysis failed ({exc}); promoting with no confidence.",
            "source": "fallback",
        })

    return a2a.to_response(verdict, pr_url=pr_url)


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    loop = asyncio.get_running_loop()
    if _semaphore is None or _semaphore[0] is not loop:
        _semaphore = (loop, asyncio.Semaphore(get_settings().a2a_max_concurrency))
    return _semaphore[1]


async def analyze(payload: dict) -> dict:
    """Bounded, deadlined wrapper around `analyze_sync`.

    On timeout the worker thread is left to finish — its checkpoint write is still useful
    to the next analysis of the same rollout — while we answer fail-open immediately.
    """
    deadline = get_settings().a2a_deadline_seconds
    try:
        async with _get_semaphore():
            try:
                return await asyncio.wait_for(asyncio.to_thread(analyze_sync, payload), deadline)
            except asyncio.TimeoutError:
                canary = a2a.canary_ref_from(payload)
                logger.warning("canary analysis exceeded %.0fs deadline for %s/%s",
                               deadline, canary.get("namespace"), canary.get("rollout"))
                return a2a.to_response({
                    "promote": True,
                    "confidence": 0,
                    "analysis": (
                        f"Analysis exceeded the {deadline:.0f}s deadline; promoting with no "
                        "confidence. Other metrics in this AnalysisTemplate still gate the "
                        "rollout."
                    ),
                    "source": "fallback",
                })
    except Exception as exc:
        # Last line of defence: anything reaching the endpoint as a non-200 fails the
        # AnalysisRun outright, which is a worse outcome than promoting unconfidently.
        logger.exception("canary analysis raised outside the guarded paths")
        return a2a.to_response({
            "promote": True,
            "confidence": 0,
            "analysis": f"Analysis errored ({exc}); promoting with no confidence.",
            "source": "fallback",
        })
