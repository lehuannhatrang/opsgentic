from __future__ import annotations

import asyncio
import json
import logging

from opsgentic.agent_skills import render
from opsgentic.agents.llm import get_llm
from opsgentic.config import get_settings
from opsgentic.mcp.loader import load_connections

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

# Per-tool-call result kept for the audit trail (context_data.tool_calls). Long enough
# to debug a PromQL/k8s response, short enough to keep the checkpoint/API payload sane.
_AUDIT_RESULT_MAX_CHARS = 2000

# Prometheus metric labels that add noise without diagnostic value — dropped when
# flattening a result so the agent (and auditor) sees value-first, readable output.
_PROM_DROP_LABELS = {
    "id", "image", "endpoint", "instance", "job", "metrics_path",
    "name", "service", "uid", "container_id", "prometheus", "prometheus_replica",
}


def _extract_text(raw: object) -> str:
    """Pull the text payload out of an MCP tool result, whatever shape it arrives in:
    a plain string, a (content, artifact) tuple, or a list of content blocks
    ([{'type':'text','text': ...}])."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, tuple):
        return _extract_text(raw[0]) if raw else ""
    if isinstance(raw, list):
        parts = []
        for item in raw:
            if isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
            elif isinstance(item, str):
                parts.append(item)
            else:
                parts.append(str(item))
        return "".join(parts)
    if isinstance(raw, dict) and "text" in raw:
        return str(raw["text"])
    return str(raw)


def _is_number(x: object) -> bool:
    try:
        float(x)  # type: ignore[arg-type]
        return True
    except (TypeError, ValueError):
        return False


def _fmt_labels(metric: dict) -> str:
    items = sorted((k, v) for k, v in (metric or {}).items() if k not in _PROM_DROP_LABELS)
    return "{" + ", ".join(f"{k}={v}" for k, v in items) + "}"


def _flatten_prometheus_result(raw: object) -> str:
    """Turn a pab1it0 prometheus-mcp result into compact, value-first text. Vector ->
    one `labels = value` line per series; matrix (range) -> per-series last/min/max/n.
    Drops the `links` UI block and noisy labels. Any unexpected shape (metadata,
    list_metrics, parse failure) is returned verbatim so we never lose information."""
    text = _extract_text(raw)
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return text
    if not isinstance(data, dict):
        return text
    rtype = data.get("resultType")
    series = data.get("result")
    if rtype == "scalar" or rtype == "string":
        return f"{rtype}: {series}"
    if rtype not in ("vector", "matrix") or not isinstance(series, list):
        return text  # not a query result (e.g. metadata/labels) -> leave as-is
    lines = []
    for s in series:
        labels = _fmt_labels(s.get("metric", {}))
        if rtype == "vector":
            val = (s.get("value") or [None, None])[1]
            lines.append(f"  {labels} = {val}")
        else:  # matrix
            vals = s.get("values") or []
            nums = [float(v[1]) for v in vals if isinstance(v, (list, tuple)) and len(v) > 1 and _is_number(v[1])]
            if nums:
                last = vals[-1][1]
                lines.append(f"  {labels} -> last={last} min={min(nums):.6g} max={max(nums):.6g} n={len(vals)}")
            else:
                lines.append(f"  {labels} -> n={len(vals)}")
    head = f"{rtype}, {len(series)} series:"
    return head + "\n" + "\n".join(lines) if lines else f"{head}\n  (no data)"


def _wrap_prometheus_tool(tool):
    """Wrap a Prometheus MCP tool so its output is flattened to readable, value-first
    text before it reaches the agent (and the audit trail). On any error, the original
    tool result text is preserved."""
    from langchain_core.tools import StructuredTool

    async def _run(**kwargs):
        raw = await tool.ainvoke(kwargs)
        try:
            return _flatten_prometheus_result(raw)
        except Exception:  # never let formatting drop the underlying data
            logger.warning("prometheus result flatten failed for %s", tool.name, exc_info=True)
            return _extract_text(raw)

    return StructuredTool(
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
        coroutine=_run,
    )


def _summarize_tool_calls(messages: list, tool_server: dict[str, str] | None = None) -> list[dict]:
    """Build an ordered audit trail of every tool the agent invoked: the MCP server it
    belongs to (kubernetes/prometheus/github), name, the args it chose (e.g. the
    PromQL/time range), and the (truncated) result. Drives both the INFO log and
    context_data.tool_calls so a run can be debugged from the console."""
    tool_server = tool_server or {}
    # Pair each tool call with its result message via tool_call_id.
    results = {
        getattr(m, "tool_call_id", None): m
        for m in messages
        if getattr(m, "type", "") == "tool"
    }
    calls: list[dict] = []
    for m in messages:
        for tc in getattr(m, "tool_calls", None) or []:
            tm = results.get(tc.get("id"))
            content = getattr(tm, "content", "") if tm is not None else ""
            if not isinstance(content, str):
                content = str(content)
            calls.append({
                "server": tool_server.get(tc.get("name"), "unknown"),
                "name": tc.get("name"),
                "args": tc.get("args") or {},
                "result": content[:_AUDIT_RESULT_MAX_CHARS],
                "result_truncated": len(content) > _AUDIT_RESULT_MAX_CHARS,
                "status": getattr(tm, "status", None) if tm is not None else "no_result",
            })
    return calls


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
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from langgraph.prebuilt import create_react_agent

    connections = load_connections(include={"kubernetes", "prometheus"})
    if not connections:
        return _unavailable("no MCP servers configured")

    client = MultiServerMCPClient(connections)
    # Fetch tools per server so each tool's origin (kubernetes/prometheus/github) is known
    # for the audit trail; the agent still sees one merged tool list.
    tool_server: dict[str, str] = {}
    tools = []
    for server_name in connections:
        for t in await client.get_tools(server_name=server_name):
            tool_server[t.name] = server_name
            if t.name in _CONTEXT_TOOL_DENYLIST:
                continue
            tools.append(_wrap_prometheus_tool(t) if server_name == "prometheus" else t)

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
    tool_calls = _summarize_tool_calls(messages, tool_server)
    for c in tool_calls:
        logger.info(
            "context tool call: [%s] %s args=%s -> %s%s",
            c["server"], c["name"], c["args"], c["result"][:200],
            "…(truncated)" if c["result_truncated"] else "",
        )
    return {
        "source": "mcp",
        "namespace": labels.get("namespace"),
        "summary": summary if isinstance(summary, str) else str(summary),
        "tools_used": sorted({c["name"] for c in tool_calls if c["name"]}),
        "tool_calls": tool_calls,
    }


def _unavailable(reason: str) -> dict:
    return {"source": "stub", "note": f"context enrichment unavailable ({reason})"}
