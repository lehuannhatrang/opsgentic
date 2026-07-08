from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from opsgentic.config import get_settings

_END = "END"
# Tried after the configured path so local/dev and tests resolve the shipped spec from
# the repo root without setting PIPELINE_CONFIG_PATH.
_FALLBACK = "config/pipeline.yaml"


class PipelineSpecError(ValueError):
    """Raised when the pipeline spec is missing or structurally invalid."""


@dataclass(frozen=True)
class NodeSpec:
    id: str
    step: str
    agents: tuple[str, ...]


@dataclass(frozen=True)
class EdgeSpec:
    source: str
    target: str | None = None            # static edge target (mutually exclusive with route)
    route: str | None = None             # named router -> conditional edge
    branches: dict[str, str] = field(default_factory=dict)

    @property
    def conditional(self) -> bool:
        return self.route is not None


@dataclass(frozen=True)
class PipelineSpec:
    entrypoint: str
    interrupt_before: tuple[str, ...]
    nodes: tuple[NodeSpec, ...]
    edges: tuple[EdgeSpec, ...]
    agent_tools: dict[str, frozenset[str]]
    off_graph_tools: dict[str, frozenset[str]]

    @property
    def node_agents(self) -> dict[str, list[str]]:
        return {n.id: list(n.agents) for n in self.nodes}

    @property
    def off_graph_agents(self) -> list[str]:
        return list(self.off_graph_tools.keys())


def _tools(cfg: dict | None) -> frozenset[str]:
    return frozenset(str(t) for t in (cfg or {}).get("tools", []))


def parse_spec(raw: dict) -> PipelineSpec:
    if not isinstance(raw, dict):
        raise PipelineSpecError("pipeline spec must be a mapping")

    nodes = tuple(
        NodeSpec(
            id=str(n["id"]),
            step=str(n["step"]),
            agents=tuple(str(a) for a in (n.get("agents") or [])),
        )
        for n in (raw.get("nodes") or [])
    )
    if not nodes:
        raise PipelineSpecError("pipeline spec has no nodes")

    edges = tuple(
        EdgeSpec(
            source=str(e["from"]),
            target=(str(e["to"]) if e.get("to") is not None else None),
            route=(str(e["route"]) if e.get("route") is not None else None),
            branches={str(k): str(v) for k, v in (e.get("branches") or {}).items()},
        )
        for e in (raw.get("edges") or [])
    )

    agent_tools = {str(a): _tools(cfg) for a, cfg in (raw.get("agents") or {}).items()}
    off_graph_tools = {str(a): _tools(cfg) for a, cfg in (raw.get("off_graph") or {}).items()}

    spec = PipelineSpec(
        entrypoint=str(raw.get("entrypoint") or ""),
        interrupt_before=tuple(str(x) for x in (raw.get("interrupt_before") or [])),
        nodes=nodes,
        edges=edges,
        agent_tools=agent_tools,
        off_graph_tools=off_graph_tools,
    )
    _validate(spec)
    return spec


def _validate(spec: PipelineSpec) -> None:
    node_ids = {n.id for n in spec.nodes}
    if spec.entrypoint not in node_ids:
        raise PipelineSpecError(f"entrypoint {spec.entrypoint!r} is not a declared node")
    for n in spec.interrupt_before:
        if n not in node_ids:
            raise PipelineSpecError(f"interrupt_before names unknown node {n!r}")

    valid_targets = node_ids | {_END}
    for e in spec.edges:
        if e.source not in node_ids:
            raise PipelineSpecError(f"edge 'from' {e.source!r} is not a declared node")
        if e.conditional:
            if not e.branches:
                raise PipelineSpecError(f"conditional edge from {e.source!r} has no branches")
            for label, tgt in e.branches.items():
                if tgt not in valid_targets:
                    raise PipelineSpecError(
                        f"branch {label!r} of {e.source!r} targets unknown node {tgt!r}"
                    )
        elif e.target not in valid_targets:
            raise PipelineSpecError(f"edge from {e.source!r} targets unknown node {e.target!r}")

    for n in spec.nodes:
        for a in n.agents:
            if a not in spec.agent_tools:
                raise PipelineSpecError(
                    f"node {n.id!r} references agent {a!r} with no 'agents:' entry"
                )


def _resolve_path() -> Path:
    configured = getattr(get_settings(), "pipeline_config_path", _FALLBACK)
    for candidate in (configured, _FALLBACK):
        p = Path(candidate)
        if p.exists():
            return p
    raise PipelineSpecError(
        f"pipeline spec not found (tried {configured!r}, {_FALLBACK!r})"
    )


@lru_cache
def load_spec() -> PipelineSpec:
    """Load and validate the pipeline spec once (cached). Edits require a process restart,
    consistent with the agent-skill loader."""
    raw = yaml.safe_load(_resolve_path().read_text()) or {}
    return parse_spec(raw)
