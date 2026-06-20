from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Optional

from opsgentic.config import get_settings

if TYPE_CHECKING:
    from langchain_openai import ChatOpenAI


@lru_cache
def get_llm() -> Optional["ChatOpenAI"]:
    """ChatOpenAI pointing at vLLM (OpenAI-compatible). None if not configured."""
    settings = get_settings()
    if not settings.llm_base_url:
        return None

    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key or "EMPTY",
        model=settings.llm_model,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
    )
