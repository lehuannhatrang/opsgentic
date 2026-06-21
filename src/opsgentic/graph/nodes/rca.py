from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from opsgentic.agent_skills import render
from opsgentic.agents.llm import get_llm
from opsgentic.graph.state import MachineState
from opsgentic.mcp.context import gather_context

# Used when the agent-skills library is missing (the system prompt comes from the
# 'sre' skill wired to the 'rca' agent).
_FALLBACK = (
    "You are an SRE Root Cause Analysis agent. Given an alert payload and "
    "cluster context, produce the single most likely root cause hypothesis. "
    "Be specific and reference the evidence you used."
)


def rca_node(state: MachineState) -> dict:
    alert = state.get("alert_payload", {})
    # Reuse caller-supplied context if present; otherwise enrich via read-only MCP.
    context = state.get("context_data") or gather_context(alert)
    attempts = state.get("rca_attempts", 0) + 1

    llm = get_llm()
    if llm is None:
        hypothesis = (
            f"[stub] Likely cause for '{alert.get('title', 'unknown alert')}': "
            "resource saturation or a recent rollout (LLM not configured)."
        )
    else:
        prompt = (
            f"Alert:\n{alert}\n\nContext:\n{context}\n\n"
            "Return the single most likely root cause."
        )
        resp = llm.invoke([SystemMessage(content=render("rca", _FALLBACK)), HumanMessage(content=prompt)])
        hypothesis = resp.content if isinstance(resp.content, str) else str(resp.content)

    return {
        "context_data": context,
        "hypothesis": hypothesis,
        "rca_attempts": attempts,
        "execution_status": "pending",
        "messages": [AIMessage(content=f"RCA hypothesis: {hypothesis}")],
    }
