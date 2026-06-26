from __future__ import annotations

import json
import logging

from opsgentic.mcp.loader import load_connections

logger = logging.getLogger(__name__)

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


def _auth_github(connections: dict) -> None:
    """Inject a fresh GitHub App installation token into the github MCP connection.
    github-mcp-server HTTP mode authenticates per-request via Authorization: Bearer,
    and installation tokens are short-lived, so mint one at call time."""
    conn = connections.get("github")
    if not conn:
        return
    from opsgentic.gitops.github_app import github_app_token

    token = github_app_token()
    if token:
        headers = dict(conn.get("headers") or {})
        headers["Authorization"] = f"Bearer {token}"
        connections["github"] = {**conn, "headers": headers}


async def load_tools(include: set[str], *, deny: set[str] | None = None) -> tuple[list, dict[str, str]]:
    """Load read-only MCP tools for the given servers and return (tools, tool_server).

    Tools are fetched per server so each tool's origin (kubernetes/prometheus/github) is
    known for the audit trail; the agent still sees one merged list. Prometheus tools are
    wrapped to flatten their output (see `_wrap_prometheus_tool`). `deny` drops high-volume
    tools by name. Returns ([], {}) when no matching servers are configured."""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    deny = deny or set()
    connections = load_connections(include=include)
    if not connections:
        return [], {}
    _auth_github(connections)

    client = MultiServerMCPClient(connections)
    tool_server: dict[str, str] = {}
    tools: list = []
    for server_name in connections:
        for t in await client.get_tools(server_name=server_name):
            tool_server[t.name] = server_name
            if t.name in deny:
                continue
            tools.append(_wrap_prometheus_tool(t) if server_name == "prometheus" else t)
    return tools, tool_server


def summarize_tool_calls(messages: list, tool_server: dict[str, str] | None = None) -> list[dict]:
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


def log_tool_calls(prefix: str, tool_calls: list[dict]) -> None:
    """Emit one INFO line per tool call (server, name, args, truncated result)."""
    for c in tool_calls:
        logger.info(
            "%s tool call: [%s] %s args=%s -> %s%s",
            prefix, c["server"], c["name"], c["args"], c["result"][:200],
            "…(truncated)" if c["result_truncated"] else "",
        )
