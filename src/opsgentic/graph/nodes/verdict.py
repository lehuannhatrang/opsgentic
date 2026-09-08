"""Canary verdict node.

Turns the RCA hypothesis and validation report into the promote/abort decision the Argo
Rollouts AI metric plugin expects. The plugin has no Inconclusive phase: `promote: false`
aborts a live rollout. So this node only votes to abort on positive evidence that the
canary is causing harm; every uncertain path promotes with low confidence and explains
itself instead.
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from opsgentic.agent_skills import render
from opsgentic.agents.llm import get_llm
from opsgentic.graph.state import MachineState

logger = logging.getLogger(__name__)

_FALLBACK = (
    "You are an SRE judging whether a canary deployment should be promoted or aborted.\n"
    "You are given a root-cause hypothesis, a deterministic validation report, and cluster "
    "context for the canary under analysis.\n\n"
    "Decide promote=false ONLY when the evidence positively shows the canary itself is "
    "causing harm — elevated errors, crashes, or saturation attributable to the new "
    "revision. Ambiguity, missing telemetry, a pre-existing problem, or a hypothesis you "
    "cannot corroborate are all reasons to promote=true with LOW confidence, not to abort.\n\n"
    "Aborting rolls back a live deployment, so a wrong abort is expensive and visible. "
    "Other metrics in the same AnalysisTemplate independently gate this rollout, so you are "
    "not the last line of defence.\n\n"
    "confidence is 0-100 and describes how sure you are of the decision you returned."
)


class VerdictAnswer(BaseModel):
    promote: bool = Field(
        description="false ONLY with positive evidence the canary is causing harm; "
        "true (with low confidence) whenever uncertain"
    )
    confidence: int = Field(default=0, description="0-100 confidence in this decision")
    analysis: str = Field(default="", description="what the evidence shows, 1-3 sentences")
    root_cause: str = Field(default="", description="the cause, if identified")
    remediation: str = Field(default="", description="the concrete fix, if any")


def _fail_open(reason: str, source: str = "fallback") -> dict:
    return {
        "promote": True,
        "confidence": 0,
        "analysis": reason,
        "root_cause": "",
        "remediation": "",
        "source": source,
    }


def verdict_node(state: MachineState) -> dict:
    canary = state.get("canary_ref") or {}
    hypothesis = state.get("hypothesis")
    report = state.get("validation_report") or {}
    precheck = state.get("precheck") or {}

    if not hypothesis:
        verdict = _fail_open("No root-cause hypothesis was produced; promoting with no confidence.")
        return _emit(verdict)

    llm = get_llm()
    if llm is None:
        verdict = _fail_open(
            "LLM not configured; cannot judge the canary. Promoting with no confidence. "
            f"Hypothesis was: {hypothesis}"
        )
        return _emit(verdict)

    human = (
        f"Rollout: {canary.get('rollout') or 'unknown'} "
        f"(namespace {canary.get('namespace') or 'unknown'})\n\n"
        f"Deterministic pre-check: {precheck.get('detail') or 'not run'}"
        f" [reason={precheck.get('reason')}]\n\n"
        f"Root-cause hypothesis:\n{hypothesis}\n\n"
        f"Validation report:\n{report}\n\n"
        f"Cluster context:\n{state.get('context_data')}\n\n"
    )
    extra = canary.get("extra_prompt")
    if extra:
        human += f"Operator instructions: {extra}\n\n"
    human += "Should this canary be promoted?"

    try:
        answer = llm.with_structured_output(VerdictAnswer).invoke(
            [SystemMessage(content=render("verdict", _FALLBACK)), HumanMessage(content=human)]
        )
    except Exception as exc:
        logger.warning("verdict agent failed: %s", exc)
        return _emit(_fail_open(f"Verdict agent failed ({exc}); promoting with no confidence."))

    verdict = {
        "promote": bool(answer.promote),
        "confidence": max(0, min(100, int(answer.confidence or 0))),
        "analysis": answer.analysis or "",
        "root_cause": answer.root_cause or "",
        "remediation": answer.remediation or "",
        "source": "agent",
    }
    return _emit(verdict)


def _emit(verdict: dict) -> dict:
    decision = "promote" if verdict["promote"] else "abort"
    return {
        "verdict": verdict,
        "messages": [
            AIMessage(content=f"Canary verdict: {decision} ({verdict['confidence']}%) — "
                              f"{verdict['analysis']}")
        ],
    }
