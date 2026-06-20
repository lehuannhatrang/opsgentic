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


def load_connections() -> dict:
    """Read mcp-config/servers.yaml into a MultiServerMCPClient connections dict."""
    path = Path(get_settings().mcp_config_path)
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return _expand(data.get("servers", {}) or {})
