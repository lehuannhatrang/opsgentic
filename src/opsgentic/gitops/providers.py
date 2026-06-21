from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from opsgentic.config import get_settings

# Built-in defaults; extend per-host via config/gitops.yaml.
_DEFAULTS = {
    "github.com": {"type": "github", "api_base": "https://api.github.com", "token_env": "GITHUB_TOKEN"},
    "gitlab.com": {"type": "gitlab", "api_base": "https://gitlab.com/api/v4", "token_env": "GITLAB_TOKEN"},
}


@dataclass
class ProviderConfig:
    host: str
    type: str           # github | gitea | gitlab
    api_base: str
    token: Optional[str]


def _default_api(provider_type: str, host: str) -> str:
    if provider_type == "github":
        return "https://api.github.com" if host == "github.com" else f"https://{host}/api/v3"
    if provider_type == "gitlab":
        return f"https://{host}/api/v4"
    if provider_type == "gitea":
        return f"https://{host}/api/v1"
    return f"https://{host}"


def _registry() -> dict:
    reg = {h: dict(v) for h, v in _DEFAULTS.items()}
    path = Path(get_settings().git_config_path)
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}
        for entry in data.get("providers", []) or []:
            host = (entry.get("host") or "").lower()
            if host:
                reg[host] = {
                    "type": entry.get("type", "github"),
                    "api_base": entry.get("api_base"),
                    "token_env": entry.get("token_env"),
                }
    return reg


def get_provider(host: str) -> Optional[ProviderConfig]:
    """Resolve a host to a provider + token from the registry (config/gitops.yaml + built-in
    defaults). Returns None for an unregistered host so the run escalates instead of guessing —
    add the host to config/gitops.yaml to support it."""
    host = (host or "").lower()
    entry = _registry().get(host)
    if not entry:
        return None
    api = entry.get("api_base") or _default_api(entry["type"], host)
    token = os.environ.get(entry["token_env"]) if entry.get("token_env") else None
    return ProviderConfig(host=host, type=entry["type"], api_base=api, token=token)
