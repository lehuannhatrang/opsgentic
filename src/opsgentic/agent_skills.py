from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from opsgentic.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class Skill:
    name: str
    description: str
    agents: list[str] = field(default_factory=list)
    body: str = ""


def _parse(path: Path) -> Skill | None:
    """Parse a markdown skill: optional YAML frontmatter (between --- fences) + body."""
    text = path.read_text()
    meta: dict = {}
    body = text
    if text.lstrip().startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            meta = yaml.safe_load(parts[1]) or {}
            body = parts[2]
    if not isinstance(meta, dict):
        meta = {}
    return Skill(
        name=str(meta.get("name") or path.stem),
        description=str(meta.get("description") or ""),
        agents=[str(a) for a in (meta.get("agents") or [])],
        body=body.strip(),
    )


# Tried after the configured path so local runs from the repo root find the skills
# without setting SKILLS_PATH (in-cluster the configured `agent-skills` -> /app/agent-skills
# resolves first; this repo-relative source dir does not exist in the image).
_FALLBACK_DIRS = ("deploy/manifests/agent-skills",)


def _resolve_dir() -> Path | None:
    for candidate in (get_settings().skills_path, *_FALLBACK_DIRS):
        p = Path(candidate)
        if p.exists():
            return p
    return None


@lru_cache
def _load_all() -> tuple[Skill, ...]:
    """Read every <skills>/*.md once (cached). Edits require a process restart."""
    path = _resolve_dir()
    if path is None:
        logger.warning(
            "skills dir not found (tried %s); agents fall back to built-in prompts",
            ", ".join((get_settings().skills_path, *_FALLBACK_DIRS)),
        )
        return ()
    skills: list[Skill] = []
    for f in sorted(path.glob("*.md")):
        try:
            skill = _parse(f)
            if skill and skill.body:
                skills.append(skill)
        except Exception as exc:  # malformed frontmatter -> skip this skill, keep the rest
            logger.warning("failed to parse skill %s: %s", f, exc)
    return tuple(skills)


def render(agent: str, default: str = "") -> str:
    """Compose the bodies of all skills wired to `agent` (via their frontmatter `agents:`
    field) into a single system-prompt block. Returns `default` when the library is missing
    or no skill targets this agent, so an agent never runs without a prompt."""
    bodies = [s.body for s in _load_all() if agent in s.agents]
    return "\n\n".join(bodies) if bodies else default
