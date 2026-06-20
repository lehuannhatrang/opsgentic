from __future__ import annotations

from functools import lru_cache

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load .env into the process environment so ${VAR} references in config files
# (e.g. mcp-config/servers.yaml) resolve the same locally as from K8s env vars.
# Does not override variables already set in the environment.
load_dotenv()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM (vLLM, OpenAI-compatible). Empty -> nodes use the canned fallback.
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str = "local-model"
    llm_temperature: float = 0.0
    llm_max_tokens: int = 4096

    # MCP (read-only tooling). Disabled -> RCA uses a stub context.
    mcp_enabled: bool = False
    mcp_config_path: str = "mcp-config/servers.yaml"
    mcp_recursion_limit: int = 25

    # GitOps provider registry. Per-host tokens come from each provider's token_env
    # (e.g. GITHUB_TOKEN / GITEA_TOKEN / GITLAB_TOKEN). The GIT_* fields below are a
    # legacy single-provider fallback for hosts not in the registry.
    git_config_path: str = "config/gitops.yaml"
    # GitHub App auth for the github provider (preferred over a static PAT).
    github_app_id: str | None = None
    github_app_installation_id: str | None = None
    github_app_private_key: str | None = None
    github_app_private_key_path: str | None = None
    git_provider: str = "github"          # legacy fallback type
    git_token: str | None = None          # legacy fallback token
    git_base_url: str | None = None       # legacy fallback API base

    # Checkpointing. Empty database_url -> in-memory (dev, single process).
    database_url: str | None = None
    db_pool_max_size: int = 10

    # Graph
    max_rca_attempts: int = 2
    # Auto-approve: skip the human approval gate so a passing validation opens the PR
    # automatically. Safe because the PR is not auto-merged — review/merge is the gate.
    auto_approve: bool = False

    # App
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
