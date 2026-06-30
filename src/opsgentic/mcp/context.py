from __future__ import annotations

import asyncio
import logging

from opsgentic.agent_skills import render
from opsgentic.agents.llm import get_llm
from opsgentic.config import get_settings
from opsgentic.agent_registry import AGENT_TOOLS
from opsgentic.mcp.agent_tools import load_tools, log_tool_calls, summarize_tool_calls

logger = logging.getLogger(__name__)

# Fallback when the agent-skills library is missing (the prompt normally comes from the
# 'sre' skill wired to the 'context' agent).
_FALLBACK = (
    "You are an SRE diagnostics assistant with READ-ONLY access to a Kubernetes "
    "cluster and telemetry through tools. Investigate the alert by inspecting "
    "relevant pods, events, and metrics for the affected namespace and workload. "
    "Be economical: prefer list/get/top/events over bulk dumps, and stop once you "
    "have enough evidence. Never attempt any mutating action. Summarize the "
    "concrete evidence you found."
)

# Tools whose output can be huge (full logs, kubelet stats) blow up the agent's
# context window if appended verbatim across steps. Exclude them from enrichment.
_CONTEXT_TOOL_DENYLIST = {"pods_log", "nodes_log", "nodes_stats_summary", "nodes_metrics"}


def gather_context(alert: dict) -> dict:
    """Enrich context via read-only MCP tools. Falls back to a stub when MCP is
    disabled or unreachable, so the graph keeps running in dev / without a cluster."""
    settings = get_settings()
    if not settings.mcp_enabled:
        return _unavailable("MCP disabled")
    try:
        return asyncio.run(_gather_async(alert))
    except Exception as exc:  # no cluster / binary / tool-calling support
        from opsgentic.mcp.loader import explain_exception

        detail = explain_exception(exc)
        logger.warning("MCP context enrichment failed: %s", detail, exc_info=True)
        return _unavailable(f"MCP error: {detail}")


async def _gather_async(alert: dict) -> dict:
    from langgraph.prebuilt import create_react_agent

    tools, tool_server = await load_tools(AGENT_TOOLS["context"], deny=_CONTEXT_TOOL_DENYLIST)
    if not tools:
        return _unavailable("no MCP servers configured")

    llm = get_llm()
    if llm is None:
        # No LLM to drive enrichment: at least confirm MCP connectivity.
        return {
            "source": "mcp",
            "tools_available": [t.name for t in tools],
            "note": "MCP connected; LLM not configured for enrichment",
        }

    # checkpointer=False: do NOT inherit the parent graph's checkpointer. When this
    # sub-agent runs inside _app.invoke(), a None checkpointer would inherit the parent's
    # sync PostgresSaver, whose async aget_tuple is unimplemented -> NotImplementedError.
    agent = create_react_agent(llm, tools, prompt=render("context", _FALLBACK), checkpointer=False)
    labels = alert.get("labels", {}) or {}
    human = (
        f"Alert: {alert.get('title')}\n"
        f"Description: {alert.get('description')}\n"
        f"Labels: {labels}\n\n"
        "Investigate read-only and summarize the diagnostic evidence."
    )
    result = await agent.ainvoke(
        {"messages": [("user", human)]},
        config={"recursion_limit": get_settings().mcp_recursion_limit},
    )
    messages = result.get("messages", [])
    summary = getattr(messages[-1], "content", "") if messages else ""
    tool_calls = summarize_tool_calls(messages, tool_server)
    log_tool_calls("context", tool_calls)
    return {
        "source": "mcp",
        "namespace": labels.get("namespace"),
        "summary": summary if isinstance(summary, str) else str(summary),
        "tools_used": sorted({c["name"] for c in tool_calls if c["name"]}),
        "tool_calls": tool_calls,
    }


def _unavailable(reason: str) -> dict:
    return {"source": "stub", "note": f"context enrichment unavailable ({reason})"}
