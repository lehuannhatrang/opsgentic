# Pipeline Spec — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the LangGraph topology and agent↔tool wiring declarative — driven by a single `config/pipeline.yaml` — so a developer can add/remove/reorder nodes and change routing without editing orchestration Python, while runtime behavior stays byte-identical to today.

**Architecture:** Introduce a two-layer model. The **engine layer** is a code registry of named "steps" (node functions) and "routers" (`opsgentic/pipeline/registry.py`). The **blueprint layer** is `config/pipeline.yaml`, which wires those named steps/routers into a concrete graph. A typed loader (`opsgentic/pipeline/spec.py`) parses and validates the YAML; `graph/builder.py` interprets the spec to build the `StateGraph`; and `agent_registry.py` becomes a thin projection of the same spec, so the runtime graph and the console graph view can never drift from the blueprint.

**Tech Stack:** Python 3.11+, LangGraph, PyYAML, dataclasses, pydantic-settings, pytest.

---

## Repo conventions (must follow — from `.claude/rules.md`)

- **No autonomous commits.** Every task ends by **staging** changes and **printing the suggested commit command** for the user to run. Do NOT run `git commit`.
- **English only** for all code, comments, docstrings.
- **Docs on-demand.** Do not add/modify README/architecture docs in Phase 1 (blueprint docs are Phase 2).
- **Session changelog.** The final task appends a summary to `.claude/changelogs/2026-07-07.md`.
- **Secrets.** No changes to secret files; do not stage `deploy/**/secrets.yaml` or `*.pem`.

## Ground truth this refactor must preserve

These existing tests encode the current behavior and MUST stay green unchanged:

- `tests/test_agent_registry.py` — `AGENT_TOOLS`, `NODE_AGENTS`, `OFF_GRAPH_AGENTS` exact values.
- `tests/test_graphview.py` — system graph topology, conditional validation edges, tool/skill/memory edges, off-graph pr-responder, run overlays.

The default `config/pipeline.yaml` must reproduce exactly:

- Nodes: `rca`, `resolve_target`, `validation`, `action`.
- Edges: `START→rca`, `rca→resolve_target`, `resolve_target→validation`, conditional `validation→{action|rca|END}` (router `after_validation`), `action→END`.
- `interrupt_before=["action"]`.
- `AGENT_TOOLS`: `context={kubernetes,prometheus}`, `rca={}`, `resolver={}`, `validation={}`, `remediation={kubernetes,github,prometheus}`, `pr-responder={kubernetes,prometheus,github}`.
- `NODE_AGENTS`: `rca=[context,rca]`, `resolve_target=[resolver]`, `validation=[validation]`, `action=[remediation]`.
- `OFF_GRAPH_AGENTS=[pr-responder]`.

## File Structure

- Create `src/opsgentic/pipeline/__init__.py` — new subpackage marker.
- Create `src/opsgentic/pipeline/spec.py` — typed spec dataclasses + YAML loader + structural validation. No dependency on the graph/registry (avoids import cycles).
- Create `src/opsgentic/pipeline/registry.py` — `STEP_REGISTRY` (name→node fn) and `ROUTER_REGISTRY` (name→router fn); hosts the `after_validation` router moved out of `builder.py`.
- Create `config/pipeline.yaml` — the default blueprint (matches current topology). Baked into the image by the existing `COPY config ./config` in the Dockerfile; no deploy change needed.
- Modify `src/opsgentic/config.py` — add `pipeline_config_path` setting.
- Modify `src/opsgentic/graph/builder.py` — `build_app` interprets the spec (generic wiring) instead of hard-coded nodes/edges.
- Modify `src/opsgentic/agent_registry.py` — derive `AGENT_TOOLS`/`NODE_AGENTS`/`OFF_GRAPH_AGENTS` from `load_spec()`; keep the public names so `graphview.py`, `mcp/context.py`, `conversation/responder.py` keep importing unchanged.
- Create `tests/test_pipeline_spec.py`, `tests/test_pipeline_registry.py`, `tests/test_builder_pipeline.py`.

No `pyproject.toml` change: hatchling `packages = ["src/opsgentic"]` includes the new subpackage automatically.

---

### Task 1: Typed pipeline spec + loader (`spec.py`)

**Files:**
- Create: `src/opsgentic/pipeline/__init__.py`
- Create: `src/opsgentic/pipeline/spec.py`
- Test: `tests/test_pipeline_spec.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pipeline_spec.py`:

```python
import pytest

from opsgentic.pipeline import spec as specmod


def _minimal_raw():
    return {
        "entrypoint": "a",
        "interrupt_before": ["b"],
        "nodes": [
            {"id": "a", "step": "rca", "agents": ["context"]},
            {"id": "b", "step": "action", "agents": ["remediation"]},
        ],
        "edges": [
            {"from": "a", "route": "after_validation", "branches": {"go": "b", "stop": "END"}},
            {"from": "b", "to": "END"},
        ],
        "agents": {"context": {"tools": ["kubernetes"]}, "remediation": {"tools": []}},
        "off_graph": {"pr-responder": {"tools": ["github"]}},
    }


def test_parse_produces_typed_spec():
    s = specmod.parse_spec(_minimal_raw())
    assert s.entrypoint == "a"
    assert s.interrupt_before == ("b",)
    assert [n.id for n in s.nodes] == ["a", "b"]
    assert s.node_agents == {"a": ["context"], "b": ["remediation"]}
    assert s.agent_tools["context"] == frozenset({"kubernetes"})
    assert s.off_graph_agents == ["pr-responder"]
    cond = [e for e in s.edges if e.conditional]
    assert cond and cond[0].branches == {"go": "b", "stop": "END"}
    straight = [e for e in s.edges if not e.conditional]
    assert straight and straight[0].source == "b" and straight[0].target == "END"


def test_missing_entrypoint_node_rejected():
    raw = _minimal_raw()
    raw["entrypoint"] = "nope"
    with pytest.raises(specmod.PipelineSpecError):
        specmod.parse_spec(raw)


def test_edge_to_unknown_node_rejected():
    raw = _minimal_raw()
    raw["edges"][1]["to"] = "ghost"
    with pytest.raises(specmod.PipelineSpecError):
        specmod.parse_spec(raw)


def test_conditional_branch_to_unknown_node_rejected():
    raw = _minimal_raw()
    raw["edges"][0]["branches"]["go"] = "ghost"
    with pytest.raises(specmod.PipelineSpecError):
        specmod.parse_spec(raw)


def test_node_agent_without_entry_rejected():
    raw = _minimal_raw()
    raw["nodes"][0]["agents"] = ["mystery"]
    with pytest.raises(specmod.PipelineSpecError):
        specmod.parse_spec(raw)


def test_no_nodes_rejected():
    with pytest.raises(specmod.PipelineSpecError):
        specmod.parse_spec({"entrypoint": "a", "nodes": [], "edges": [], "agents": {}})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_pipeline_spec.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'opsgentic.pipeline'`.

- [ ] **Step 3: Create the subpackage marker**

Create `src/opsgentic/pipeline/__init__.py` (empty file):

```python
```

- [ ] **Step 4: Implement `spec.py`**

Create `src/opsgentic/pipeline/spec.py`:

```python
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
    for candidate in (get_settings().pipeline_config_path, _FALLBACK):
        p = Path(candidate)
        if p.exists():
            return p
    raise PipelineSpecError(
        f"pipeline spec not found (tried {get_settings().pipeline_config_path!r}, {_FALLBACK!r})"
    )


@lru_cache
def load_spec() -> PipelineSpec:
    """Load and validate the pipeline spec once (cached). Edits require a process restart,
    consistent with the agent-skill loader."""
    raw = yaml.safe_load(_resolve_path().read_text()) or {}
    return parse_spec(raw)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_pipeline_spec.py -v`
Expected: all PASS except `test_shipped_default_spec_loads` is not in this file (it lands in Task 3). All six tests here PASS.

Note: `test_node_agent_without_entry_rejected` passes because node `a` now references `mystery`, absent from `agents`.

- [ ] **Step 6: Checkpoint (stage + suggest commit — user runs it)**

Run: `git add src/opsgentic/pipeline/__init__.py src/opsgentic/pipeline/spec.py tests/test_pipeline_spec.py`
Then print for the user to run:

```bash
git commit -m "feat(pipeline): typed pipeline spec loader with structural validation"
```

---

### Task 2: Step & router registries (`registry.py`)

**Files:**
- Create: `src/opsgentic/pipeline/registry.py`
- Test: `tests/test_pipeline_registry.py`

This moves the router `_route_after_validation` out of `graph/builder.py` verbatim and registers the four node functions by name. The router logic is copied exactly (branch on plan presence, `failed`→escalate, else loop to rca).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pipeline_registry.py`:

```python
from opsgentic.pipeline.registry import ROUTER_REGISTRY, STEP_REGISTRY


def test_step_registry_has_all_default_steps():
    assert {"rca", "resolve_target", "validation", "action"} <= set(STEP_REGISTRY)
    for fn in STEP_REGISTRY.values():
        assert callable(fn)


def test_after_validation_router_matches_legacy_logic():
    route = ROUTER_REGISTRY["after_validation"]
    # passed + plan -> action
    assert route({"validation_report": {"passed": True}, "remediation_plan": {"x": 1}}) == "action"
    # passed but no plan -> self-heal loop back to rca
    assert route({"validation_report": {"passed": True}}) == "rca"
    # exhausted / unresolved -> escalate
    assert route({"execution_status": "failed"}) == "escalate"
    # nothing decided -> loop
    assert route({}) == "rca"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_pipeline_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'opsgentic.pipeline.registry'`.

- [ ] **Step 3: Implement `registry.py`**

Create `src/opsgentic/pipeline/registry.py`:

```python
from __future__ import annotations

from collections.abc import Callable

from opsgentic.graph.nodes.action import action_node
from opsgentic.graph.nodes.rca import rca_node
from opsgentic.graph.nodes.resolve import resolve_target_node
from opsgentic.graph.nodes.validation import validation_node
from opsgentic.graph.state import MachineState

# Named step implementations a pipeline node binds to via `step:` in config/pipeline.yaml.
# Adding a genuinely new agent = add its node function here, then reference it from the spec.
STEP_REGISTRY: dict[str, Callable] = {
    "rca": rca_node,
    "resolve_target": resolve_target_node,
    "validation": validation_node,
    "action": action_node,
}


def _route_after_validation(state: MachineState) -> str:
    report = state.get("validation_report") or {}
    # Route on plan presence (not transient status) so update_state on approve/reject
    # does not re-route this edge and loop back to RCA.
    if report.get("passed") and state.get("remediation_plan"):
        return "action"
    if state.get("execution_status") == "failed":   # retries exhausted or unresolved repo
        return "escalate"
    return "rca"                                     # self-heal loop


# Named routers a conditional edge binds to via `route:` in config/pipeline.yaml.
ROUTER_REGISTRY: dict[str, Callable] = {
    "after_validation": _route_after_validation,
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_pipeline_registry.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Checkpoint (stage + suggest commit — user runs it)**

Run: `git add src/opsgentic/pipeline/registry.py tests/test_pipeline_registry.py`
Then print for the user to run:

```bash
git commit -m "feat(pipeline): step and router registries (named engine layer)"
```

---

### Task 3: Default blueprint file + config setting

**Files:**
- Create: `config/pipeline.yaml`
- Modify: `src/opsgentic/config.py:38` (add setting after `skills_path`)
- Test: append to `tests/test_pipeline_spec.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_pipeline_spec.py`:

```python
def test_shipped_default_spec_loads_and_matches_topology():
    specmod.load_spec.cache_clear()
    s = specmod.load_spec()
    assert {n.id for n in s.nodes} == {"rca", "resolve_target", "validation", "action"}
    assert s.entrypoint == "rca"
    assert s.interrupt_before == ("action",)
    assert s.node_agents["rca"] == ["context", "rca"]
    assert s.node_agents["action"] == ["remediation"]
    assert s.agent_tools["context"] == frozenset({"kubernetes", "prometheus"})
    assert s.agent_tools["remediation"] == frozenset({"kubernetes", "github", "prometheus"})
    assert s.off_graph_agents == ["pr-responder"]
    assert s.off_graph_tools["pr-responder"] == frozenset({"kubernetes", "prometheus", "github"})
    # conditional validation edge -> action / rca / END
    cond = [e for e in s.edges if e.conditional and e.source == "validation"]
    assert cond and cond[0].route == "after_validation"
    assert cond[0].branches == {"action": "action", "rca": "rca", "escalate": "END"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_pipeline_spec.py::test_shipped_default_spec_loads_and_matches_topology -v`
Expected: FAIL — `PipelineSpecError: pipeline spec not found` (file does not exist yet).

- [ ] **Step 3: Create `config/pipeline.yaml`**

Create `config/pipeline.yaml`:

```yaml
# OpsGentic pipeline blueprint.
#
# Declares the agent graph topology and per-agent MCP tool wiring. Edit this file to reshape
# the pipeline -- add/remove/reorder nodes, change routing, rewire tools -- without touching
# orchestration Python. `step` and `route` reference implementations registered in code
# (src/opsgentic/pipeline/registry.py: STEP_REGISTRY / ROUTER_REGISTRY). Agent behavior
# (prompts) is tuned separately in the agent-skills markdown library.
#
# NOTE: the self-heal loop cap currently lives in the MAX_RCA_ATTEMPTS env setting; it is not
# yet expressed here (planned for a later phase).
version: 1

entrypoint: rca
interrupt_before: [action]   # human-in-the-loop approval gate before opening a PR

nodes:
  - {id: rca,            step: rca,            agents: [context, rca]}
  - {id: resolve_target, step: resolve_target, agents: [resolver]}
  - {id: validation,     step: validation,     agents: [validation]}
  - {id: action,         step: action,         agents: [remediation]}

edges:
  - {from: rca,            to: resolve_target}
  - {from: resolve_target, to: validation}
  - {from: validation, route: after_validation, branches: {action: action, rca: rca, escalate: END}}
  - {from: action,         to: END}

# Per-agent read-only MCP servers. Reasoning-only agents declare an empty list.
agents:
  context:     {tools: [kubernetes, prometheus]}
  rca:         {tools: []}
  resolver:    {tools: []}
  validation:  {tools: []}
  remediation: {tools: [kubernetes, github, prometheus]}

# Agents that run outside the alert->remediation DAG (webhook-triggered).
off_graph:
  pr-responder: {tools: [kubernetes, prometheus, github]}
```

- [ ] **Step 4: Add the config setting**

In `src/opsgentic/config.py`, immediately after the `skills_path` line (currently line 38), add:

```python
    # Pipeline blueprint: declarative graph topology + agent tool wiring (config/pipeline.yaml).
    pipeline_config_path: str = "config/pipeline.yaml"
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_pipeline_spec.py -v`
Expected: all PASS (7 tests).

- [ ] **Step 6: Checkpoint (stage + suggest commit — user runs it)**

Run: `git add config/pipeline.yaml src/opsgentic/config.py tests/test_pipeline_spec.py`
Then print for the user to run:

```bash
git commit -m "feat(pipeline): default pipeline.yaml blueprint + pipeline_config_path setting"
```

---

### Task 4: Derive `agent_registry` from the spec

**Files:**
- Modify: `src/opsgentic/agent_registry.py` (full rewrite)
- Test: `tests/test_agent_registry.py` (existing — must pass unchanged)

- [ ] **Step 1: Confirm the existing test is the spec (do NOT edit it)**

Run: `.venv/bin/python -m pytest tests/test_agent_registry.py -v`
Expected: PASS against the current hard-coded module (baseline before the rewrite).

- [ ] **Step 2: Rewrite `agent_registry.py` to project the spec**

Replace the entire contents of `src/opsgentic/agent_registry.py` with:

```python
from __future__ import annotations

# Single source of truth for agent<->node<->tool wiring, DERIVED from the declarative
# pipeline spec (config/pipeline.yaml) so the runtime graph, the tool loaders
# (mcp/context.py, conversation/responder.py) and the console graph view (graphview.py)
# can never drift from the blueprint. The public names below are unchanged so those
# importers keep working without edits.

from opsgentic.pipeline.spec import load_spec

_spec = load_spec()

# Agent -> set of MCP server names it loads read-only tools from (graph agents + off-graph).
AGENT_TOOLS: dict[str, set[str]] = {
    **{agent: set(tools) for agent, tools in _spec.agent_tools.items()},
    **{agent: set(tools) for agent, tools in _spec.off_graph_tools.items()},
}

# LangGraph node -> the agent(s) that run inside it (order = execution order in the node).
NODE_AGENTS: dict[str, list[str]] = _spec.node_agents

# Agents that run outside the alert->remediation DAG (webhook-triggered).
OFF_GRAPH_AGENTS: list[str] = _spec.off_graph_agents
```

- [ ] **Step 3: Run the existing registry test to verify it still passes**

Run: `.venv/bin/python -m pytest tests/test_agent_registry.py -v`
Expected: PASS (2 tests) — identical values, now spec-derived.

- [ ] **Step 4: Run the dependent import path to catch load-time errors**

Run: `.venv/bin/python -c "import opsgentic.graphview, opsgentic.mcp.context, opsgentic.conversation.responder; print('imports ok')"`
Expected: prints `imports ok` (these modules import `agent_registry` at module load).

- [ ] **Step 5: Checkpoint (stage + suggest commit — user runs it)**

Run: `git add src/opsgentic/agent_registry.py`
Then print for the user to run:

```bash
git commit -m "refactor(pipeline): derive agent_registry wiring from the pipeline spec"
```

---

### Task 5: Spec-driven `build_app`

**Files:**
- Modify: `src/opsgentic/graph/builder.py` (full rewrite)
- Test: `tests/test_builder_pipeline.py`, plus existing `tests/test_graphview.py` must pass

`build_app` must produce the same compiled graph as before. `action_node` has signature `(state, config)`; the builder passes the bare function reference to `add_node`, so LangGraph introspects and injects `config` — do NOT wrap it in a lambda.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_builder_pipeline.py`:

```python
import pytest
from langgraph.checkpoint.memory import MemorySaver

from opsgentic.graph.builder import build_app
from opsgentic.pipeline import spec as specmod


def test_compiled_graph_matches_default_topology():
    g = build_app(MemorySaver()).get_graph()
    ids = set(g.nodes)
    assert {"rca", "resolve_target", "validation", "action"} <= ids
    edges = {(e.source, e.target) for e in g.edges}
    assert ("__start__", "rca") in edges
    assert ("rca", "resolve_target") in edges
    assert ("resolve_target", "validation") in edges
    assert ("action", "__end__") in edges
    cond_targets = {
        e.target for e in g.edges
        if e.source == "validation" and getattr(e, "conditional", False)
    }
    assert {"action", "rca", "__end__"} <= cond_targets


def test_unknown_step_raises():
    raw = {
        "entrypoint": "x",
        "nodes": [{"id": "x", "step": "does_not_exist", "agents": []}],
        "edges": [{"from": "x", "to": "END"}],
        "agents": {},
    }
    s = specmod.parse_spec(raw)
    from opsgentic.pipeline.spec import PipelineSpecError
    with pytest.raises(PipelineSpecError):
        build_app(MemorySaver(), spec=s)


def test_unknown_router_raises():
    raw = {
        "entrypoint": "x",
        "nodes": [{"id": "x", "step": "rca", "agents": []}],
        "edges": [{"from": "x", "route": "ghost_router", "branches": {"go": "END"}}],
        "agents": {},
    }
    s = specmod.parse_spec(raw)
    from opsgentic.pipeline.spec import PipelineSpecError
    with pytest.raises(PipelineSpecError):
        build_app(MemorySaver(), spec=s)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_builder_pipeline.py -v`
Expected: `test_unknown_step_raises` / `test_unknown_router_raises` FAIL (current `build_app` ignores the `spec` kwarg — `TypeError: build_app() got an unexpected keyword argument 'spec'`). `test_compiled_graph_matches_default_topology` currently PASSES against the hard-coded builder — that is fine; it locks the topology across the rewrite.

- [ ] **Step 3: Rewrite `builder.py` to interpret the spec**

Replace the entire contents of `src/opsgentic/graph/builder.py` with:

```python
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from opsgentic.graph.state import MachineState
from opsgentic.pipeline.registry import ROUTER_REGISTRY, STEP_REGISTRY
from opsgentic.pipeline.spec import PipelineSpec, PipelineSpecError, load_spec

_END_TOKEN = "END"


def _resolve_target(name: str):
    """Map the spec's END sentinel to LangGraph's END; pass node ids through."""
    return END if name == _END_TOKEN else name


def build_app(checkpointer, spec: PipelineSpec | None = None):
    """Compile the LangGraph app from the declarative pipeline spec. Node functions and
    routers are looked up by name in the engine registries, so the topology lives in
    config/pipeline.yaml rather than in code."""
    spec = spec or load_spec()
    g = StateGraph(MachineState)

    for node in spec.nodes:
        try:
            fn = STEP_REGISTRY[node.step]
        except KeyError:
            raise PipelineSpecError(
                f"node {node.id!r} uses unknown step {node.step!r} (known: {sorted(STEP_REGISTRY)})"
            )
        # Pass the bare function so LangGraph introspects its signature: nodes taking
        # (state) and (state, config) are both supported (e.g. action_node needs config).
        g.add_node(node.id, fn)

    g.add_edge(START, spec.entrypoint)

    for e in spec.edges:
        if e.conditional:
            try:
                router = ROUTER_REGISTRY[e.route]
            except KeyError:
                raise PipelineSpecError(
                    f"edge from {e.source!r} uses unknown router {e.route!r} "
                    f"(known: {sorted(ROUTER_REGISTRY)})"
                )
            mapping = {label: _resolve_target(tgt) for label, tgt in e.branches.items()}
            g.add_conditional_edges(e.source, router, mapping)
        else:
            g.add_edge(e.source, _resolve_target(e.target))

    return g.compile(checkpointer=checkpointer, interrupt_before=list(spec.interrupt_before))
```

- [ ] **Step 4: Run the builder + graphview tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_builder_pipeline.py tests/test_graphview.py -v`
Expected: all PASS (3 builder tests + all graphview tests).

- [ ] **Step 5: Checkpoint (stage + suggest commit — user runs it)**

Run: `git add src/opsgentic/graph/builder.py tests/test_builder_pipeline.py`
Then print for the user to run:

```bash
git commit -m "refactor(pipeline): build_app interprets the pipeline spec (generic wiring)"
```

---

### Task 6: Full regression + live behavior verification

**Files:** none (verification only), then `.claude/changelogs/2026-07-07.md`

- [ ] **Step 1: Run the entire test suite**

Run: `.venv/bin/python -m pytest -v`
Expected: all tests PASS (pre-existing + the three new test files). No skips introduced by this change.

- [ ] **Step 2: Verify the CLI end-to-end (behavior unchanged)**

The graph must still run start→finish from the spec. Use the canned/local path (no LLM/cluster needed):

Run: `.venv/bin/python -m opsgentic --file examples/grafana_alert.json --source grafana --approve`
Expected: the run completes through RCA → resolve_target → validation and reaches the action gate / prints a final snapshot without error (same output shape as before the refactor). Capture the final status line.

If the console is used locally, optionally sanity-check the graph view still renders:
Run: `.venv/bin/python -c "from opsgentic import graphview; g = graphview.build_system_graph(); print(sorted({n['id'] for n in g['nodes']}))"`
Expected: includes `rca`, `resolve_target`, `validation`, `action`, `END`, `memory`, `trigger:chat`, `trigger:alert`, `pr-responder`.

- [ ] **Step 3: Confirm no stray hard-coded topology remains**

Run: `grep -rn "add_node\|add_edge\|_route_after_validation" src/opsgentic/graph/builder.py`
Expected: `add_node`/`add_edge` appear only inside the generic loop; `_route_after_validation` is NOT present in `builder.py` (it now lives in `pipeline/registry.py`).

- [ ] **Step 4: Write the session changelog**

Append to `.claude/changelogs/2026-07-07.md` (create if absent):

```markdown
# 2026-07-07

## Pipeline spec — Phase 1 (declarative topology)

- Added `src/opsgentic/pipeline/` (spec loader + step/router registries).
- Added `config/pipeline.yaml` as the declarative blueprint for graph topology and agent tool wiring; added `pipeline_config_path` setting.
- `graph/builder.py` now interprets the spec to build the StateGraph (was hard-coded nodes/edges); moved `_route_after_validation` into `pipeline/registry.py`.
- `agent_registry.py` now derives `AGENT_TOOLS` / `NODE_AGENTS` / `OFF_GRAPH_AGENTS` from the spec (public names unchanged).
- Behavior preserved: existing `test_agent_registry.py` / `test_graphview.py` pass unchanged; added `test_pipeline_spec.py`, `test_pipeline_registry.py`, `test_builder_pipeline.py`. CLI run verified end-to-end.
```

- [ ] **Step 5: Checkpoint (stage + suggest commit — user runs it)**

Run: `git add .claude/changelogs/2026-07-07.md`
Then print for the user to run:

```bash
git commit -m "docs(changelog): pipeline spec phase 1"
```

---

## Out of scope (later phases)

- `opsgentic pipeline validate` / `pipeline show` CLI subcommands (Phase 2).
- Moving node labels and the self-heal loop cap (`MAX_RCA_ATTEMPTS`) into the spec (Phase 2).
- ConfigMap delivery override of `pipeline.yaml` (Phase 2 — Phase 1 relies on the baked-in `COPY config`).
- Console graph editor / drag-drop (Phase 3).
- Blueprint author docs (on request).

## Self-review notes

- **Spec coverage:** structural spec (Task 1), engine registries (Task 2), default blueprint + setting (Task 3), spec-driven registry (Task 4), spec-driven builder (Task 5), regression + live verify (Task 6). All Phase-1 design points covered.
- **Type consistency:** `PipelineSpec.agent_tools` values are `frozenset`; `agent_registry.AGENT_TOOLS` values are `set` (converted in Task 4) to match `tests/test_agent_registry.py` equality on `set`. `node_agents` values are `list` (order preserved). `EdgeSpec.conditional` is a derived property used consistently in spec validation, builder, and tests.
- **Behavior preservation:** router logic copied verbatim; `interrupt_before` sourced from spec (`["action"]`); `action_node`'s `(state, config)` signature preserved by passing the bare callable.
