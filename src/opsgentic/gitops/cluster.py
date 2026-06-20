from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import yaml

from opsgentic.config import get_settings
from opsgentic.mcp.loader import explain_exception, load_connections

logger = logging.getLogger(__name__)

# Read-only cluster access — exclusively through the MCP server (kubernetes-mcp-server).
# No direct Kubernetes API client and no kubectl.
#
# kubernetes-mcp-server returns:
#   - resources_list -> a human-readable TABLE (wrapped as [{"type":"text","text": ...}])
#   - resources_get  -> the full object as YAML (same content-block wrapping)
# So we parse the table for (namespace, name) refs, then fetch full objects with get.


def list_resource_refs(api_version: str, kind: str, namespace: Optional[str] = None) -> list:
    """Return [{'namespace','name'}] parsed from the resources_list table."""
    text = _text(_run(lambda: _fetch(["resources_list"], _args(api_version, kind, namespace))))
    return _table_refs(text, namespace)


def get_resource(api_version: str, kind: str, name: str, namespace: Optional[str] = None) -> Optional[dict]:
    """Return the full object dict via resources_get, or None."""
    args = _args(api_version, kind, namespace)
    args["name"] = name
    obj = _structured(_text(_run(lambda: _fetch(["resources_get"], args))))
    if isinstance(obj, dict) and isinstance(obj.get("items"), list):
        return obj["items"][0] if obj["items"] else None
    return obj if isinstance(obj, dict) else None


def _args(api_version: str, kind: str, namespace: Optional[str]) -> dict:
    a = {"apiVersion": api_version, "kind": kind}
    if namespace:
        a["namespace"] = namespace
    return a


def _run(make_coro):
    if not get_settings().mcp_enabled:
        return None
    try:
        return asyncio.run(make_coro())
    except Exception as exc:
        logger.warning("MCP cluster read failed: %s", explain_exception(exc))
        return None


async def _fetch(name_options, args):
    from langchain_mcp_adapters.client import MultiServerMCPClient

    connections = load_connections(include={"kubernetes"})
    if not connections:
        return None
    tools = {t.name: t for t in await MultiServerMCPClient(connections).get_tools()}
    tool = next((tools[n] for n in name_options if n in tools), None)
    if tool is None:
        logger.warning("MCP tool not found (%s); available=%s", name_options, list(tools))
        return None
    return await tool.ainvoke(args)


def _text(raw) -> Optional[str]:
    """Unwrap MCP content blocks ([{'type':'text','text': ...}]) to a plain string."""
    if raw is None or isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return raw.get("text")
    if isinstance(raw, list):
        parts = [b["text"] for b in raw if isinstance(b, dict) and isinstance(b.get("text"), str)]
        parts += [b for b in raw if isinstance(b, str)]
        return "\n".join(parts) if parts else None
    return str(raw)


def _structured(text: Optional[str]):
    if not text:
        return None
    s = text.strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    try:
        return yaml.safe_load(s)
    except Exception:
        return None


def _table_refs(text: Optional[str], default_ns: Optional[str]) -> list:
    """Parse a kubernetes-mcp-server list table into [{'namespace','name'}].
    NAMESPACE/APIVERSION/KIND/NAME are single-token columns at the front, so their
    header index aligns with the data row even when later columns are multi-word."""
    if not text:
        return []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return []
    header = lines[0].split()
    if "NAME" not in header:
        return []
    name_i = header.index("NAME")
    ns_i = header.index("NAMESPACE") if "NAMESPACE" in header else None
    refs = []
    for line in lines[1:]:
        parts = line.split()
        if len(parts) <= name_i:
            continue
        refs.append({
            "name": parts[name_i],
            "namespace": parts[ns_i] if ns_i is not None and len(parts) > ns_i else default_ns,
        })
    return refs
