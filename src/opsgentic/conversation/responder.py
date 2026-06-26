from __future__ import annotations

import asyncio
import logging
from typing import Literal

from pydantic import BaseModel, Field

from opsgentic.agent_skills import render
from opsgentic.agents.llm import get_llm
from opsgentic.config import get_settings
from opsgentic.gitops.remediator import _FieldEdit
from opsgentic.mcp.agent_tools import load_tools, log_tool_calls, summarize_tool_calls

logger = logging.getLogger(__name__)

# Allow pods_log here (a human may ask for logs); still drop the truly huge node-level dumps.
_DENY = {"nodes_log", "nodes_stats_summary", "nodes_metrics"}

_FALLBACK = (
    "You are an SRE engineer responding to a human comment on a remediation pull request. "
    "You have READ-ONLY access to the Kubernetes cluster, Prometheus, and the Git repository "
    "via tools. Investigate to answer the comment, grounding every claim in concrete evidence "
    "(metric values, events, logs, file contents) — never a bare UI link. If the human "
    "suggests a change, verify whether it is correct against the evidence BEFORE agreeing: "
    "apply it only if it is right, otherwise reply with a specific, respectful rebuttal. "
    "Never merge; proposed edits are committed to the PR branch for human review."
)


class PRResponse(BaseModel):
    kind: Literal["answer", "agree_and_edit", "disagree"] = Field(
        description="'answer' for a question/request; 'agree_and_edit' if a human suggestion is "
        "correct and you are applying it; 'disagree' if the suggestion is wrong/unsafe"
    )
    reply: str = Field(description="the markdown comment to post back on the PR")
    edits: list[_FieldEdit] = Field(
        default_factory=list,
        description="ONLY when kind=='agree_and_edit': the minimal field edit(s) to commit to the PR branch",
    )


def respond(ctx: dict, event: dict) -> tuple[PRResponse, list[dict]]:
    """Run the read-only PR-responder agent. Returns (response, tool_calls). Always returns a
    PRResponse; degrades to a plain 'answer' on MCP/LLM failure so the webhook path can still
    post something. tool_calls is the audit trail for the console (empty on failure)."""
    try:
        return asyncio.run(_respond_async(ctx, event))
    except Exception as exc:
        from opsgentic.mcp.loader import explain_exception

        logger.warning("PR responder failed: %s", explain_exception(exc), exc_info=True)
        return PRResponse(
            kind="answer",
            reply="I hit an error investigating this request and couldn't complete it. "
            "Please retry, or check the worker logs.",
        ), []


async def _respond_async(ctx: dict, event: dict) -> tuple[PRResponse, list[dict]]:
    from langgraph.prebuilt import create_react_agent

    llm = get_llm()
    if llm is None:
        return PRResponse(kind="answer", reply="(LLM not configured; cannot investigate this request.)"), []

    tools, tool_server = await load_tools({"kubernetes", "prometheus", "github"}, deny=_DENY)
    agent = create_react_agent(
        llm, tools, prompt=render("pr-responder", _FALLBACK),
        response_format=PRResponse, checkpointer=False,
    )
    result = await agent.ainvoke(
        {"messages": [("user", _build_human(ctx, event))]},
        config={"recursion_limit": get_settings().mcp_recursion_limit},
    )
    tool_calls = summarize_tool_calls(result.get("messages", []), tool_server)
    log_tool_calls("pr-responder", tool_calls)

    resp = result.get("structured_response")
    if resp is not None:
        return resp, tool_calls
    # Structured parse failed (often a truncated final generation). Fall back to the last
    # message text so the human still gets a reply.
    msgs = result.get("messages", [])
    text = getattr(msgs[-1], "content", "") if msgs else ""
    fallback = text if isinstance(text, str) and text else "(no response generated)"
    return PRResponse(kind="answer", reply=fallback), tool_calls


def _build_human(ctx: dict, event: dict) -> str:
    svc = ctx.get("service_ref") or {}
    where = "/".join(p for p in (svc.get("namespace"), svc.get("name")) if p) or "(unknown workload)"
    recent = "\n".join(
        f"- {c.get('login') or 'user'}: {(c.get('body') or '').strip()[:500]}"
        for c in (ctx.get("comments") or [])[-8:]
    )
    return (
        f"A human commented on remediation PR {ctx.get('pr_info', {}).get('url')}.\n\n"
        f"## The comment to respond to\n{event.get('comment_body', '')}\n\n"
        f"## Affected workload\n{where}\n\n"
        f"## Original alert\n{ctx.get('alert') or '(not available)'}\n\n"
        f"## Root cause hypothesis\n{ctx.get('hypothesis') or '(not available)'}\n\n"
        f"## What this PR already proposes (base...branch diff)\n{ctx.get('proposed_diff') or '(none)'}\n\n"
        f"## PR description\n{(ctx.get('pr_body') or '')[:2000]}\n\n"
        f"## Recent conversation\n{recent or '(none)'}\n\n"
        "Investigate as needed (cluster, metrics, logs, repo), then respond. If the comment "
        "proposes a change, verify it against evidence first: agree and provide minimal edits "
        "only if correct; otherwise disagree with specific reasoning."
    )
