from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM (vLLM, OpenAI-compatible). Empty -> nodes use the canned fallback.
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str = "local-model"
    llm_temperature: float = 0.0
    llm_max_tokens: int = 4096

    # GitOps
    git_provider: str = "github"          # github | gitlab
    git_token: str | None = None
    git_base_url: str | None = None       # GitHub Enterprise / GitLab self-hosted

    # Graph
    max_rca_attempts: int = 2

    # App
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
