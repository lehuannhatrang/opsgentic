from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from opsgentic.config import get_settings


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(value)        # allow ${ENV_VAR} in urls/tokens
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def load_connections(include: set | None = None) -> dict:
    """Read mcp-config/servers.yaml into a MultiServerMCPClient connections dict.

    Pass `include` (a set of server names) to select a subset — used to isolate
    failures (e.g. cluster reads only need the kubernetes server, not github)."""
    path = Path(get_settings().mcp_config_path)
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    servers = _expand(data.get("servers", {}) or {})
    if include is not None:
        servers = {k: v for k, v in servers.items() if k in include}
    return servers


def explain_exception(exc: BaseException) -> str:
    """Flatten ExceptionGroups (TaskGroup errors) to the underlying cause(s)."""
    parts: list[str] = []

    def walk(e: BaseException, depth: int = 0) -> None:
        parts.append(f"{'  ' * depth}{type(e).__name__}: {e}")
        for sub in getattr(e, "exceptions", []) or []:
            walk(sub, depth + 1)

    walk(exc)
    return " | ".join(parts)
