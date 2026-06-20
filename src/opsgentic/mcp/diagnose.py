"""Diagnose MCP connectivity. Run: python -m opsgentic.mcp.diagnose

Connects to each server in mcp-config/servers.yaml, lists its tools, and (on
failure) surfaces the real error and retries the other HTTP transport/path so you
can tell whether the server speaks streamable_http (/mcp) or sse (/sse)."""
from __future__ import annotations

import asyncio

from opsgentic.config import get_settings
from opsgentic.mcp.loader import explain_exception, load_connections


async def _probe(name: str, conn: dict) -> list[str]:
    from langchain_mcp_adapters.client import MultiServerMCPClient

    tools = await MultiServerMCPClient({name: conn}).get_tools()
    return [t.name for t in tools]


def _alternates(conn: dict) -> list[dict]:
    url = conn.get("url", "")
    if not url:
        return []
    base = url.rsplit("/", 1)[0] if "://" in url and url.count("/") > 2 else url.rstrip("/")
    out = []
    if conn.get("transport") != "streamable_http":
        out.append({"transport": "streamable_http", "url": f"{base}/mcp"})
    if conn.get("transport") != "sse":
        out.append({"transport": "sse", "url": f"{base}/sse"})
    return out


def main() -> None:
    settings = get_settings()
    conns = load_connections()
    print(f"MCP_ENABLED={settings.mcp_enabled}  MCP_CONFIG_PATH={settings.mcp_config_path}")
    if not conns:
        print("No MCP servers configured.")
        return

    for name, conn in conns.items():
        print(f"\n=== {name}: transport={conn.get('transport')} url={conn.get('url')} ===")
        try:
            tools = asyncio.run(_probe(name, conn))
            print(f"OK — {len(tools)} tools: {tools}")
            continue
        except Exception as exc:
            print("FAILED: " + explain_exception(exc))

        for alt in _alternates(conn):
            print(f"-- retry transport={alt['transport']} url={alt['url']}")
            try:
                tools = asyncio.run(_probe(name, alt))
                print(f"   OK — works as {alt['transport']} ({len(tools)} tools): {tools}")
                print(f"   -> set servers.yaml transport={alt['transport']} and K8S_MCP_URL={alt['url']}")
                break
            except Exception as exc:
                print("   FAILED: " + explain_exception(exc))


if __name__ == "__main__":
    main()
