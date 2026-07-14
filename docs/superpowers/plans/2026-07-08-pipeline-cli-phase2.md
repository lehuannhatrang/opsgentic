# Pipeline CLI & Blueprint Docs — Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give developers first-class tooling for the declarative pipeline blueprint: `opsgentic pipeline validate` and `opsgentic pipeline show` CLI subcommands, clear error messages for malformed specs, and docs with a worked "add a node" example.

**Architecture:** Add an uncached `load_spec_file(path)` to the spec loader (wrapping missing-file / malformed-YAML / missing-key into `PipelineSpecError`), a pure `render_pipeline(spec) -> str` formatter, and a non-breaking `pipeline` subcommand dispatched from the existing `opsgentic` CLI. `opsgentic --file …` keeps working exactly as before.

**Tech Stack:** Python 3.11+, argparse, PyYAML, pytest.

---

## Repo conventions (must follow — from `.claude/rules.md`)

- **No autonomous commits/staging.** Each task ends by printing the suggested `git add`/`git commit` for the user to run. Do NOT run git write commands.
- **English only** for code/comments/docstrings.
- **Docs on-demand:** the README/USAGE changes in Task 4 are explicitly in scope for this phase (user requested blueprint docs).
- **Session changelog:** Task 5 appends to `.claude/changelogs/2026-07-08.md`.
- Run tests with `.venv/bin/python -m pytest`.

## Context from Phase 1 (already in place)

- `src/opsgentic/pipeline/spec.py` — `parse_spec(raw)`, `@lru_cache load_spec()`, `PipelineSpecError`, `_resolve_path()`, dataclasses `NodeSpec(id, step, agents)`, `EdgeSpec(source, target, route, branches; .conditional)`, `PipelineSpec(entrypoint, interrupt_before, nodes, edges, agent_tools, off_graph_tools; .node_agents, .off_graph_agents)`. `agent_tools`/`off_graph_tools` values are `frozenset`.
- `config/pipeline.yaml` — block-style default blueprint.
- `src/opsgentic/cli.py` — flat argparse: `main()` reads `--file`, `--source {grafana,chat}`, `--approve`, runs the graph synchronously via `runner.execute_run` / `runner.execute_approve`, prints JSON.
- `[project.scripts]` maps `opsgentic = "opsgentic.cli:main"`.

## File Structure

- Modify `src/opsgentic/pipeline/spec.py` — add `load_spec_file(path)`; harden `parse_spec` missing-key handling; have `load_spec()` delegate to `load_spec_file`.
- Create `src/opsgentic/pipeline/render.py` — `render_pipeline(spec) -> str`.
- Modify `src/opsgentic/cli.py` — add `sys` import, `_run_main(argv)` (existing behavior), `_pipeline_main(argv)`, `_cmd_validate`, `_cmd_show`, and a dispatcher in `main()`.
- Create `tests/test_pipeline_loader_file.py`, `tests/test_pipeline_render.py`, `tests/test_pipeline_cli.py`.
- Modify `README.md` — add CLI usage + "add a node" example to the blueprint section.

---

### Task 1: `load_spec_file` + hardened error messages

**Files:**
- Modify: `src/opsgentic/pipeline/spec.py`
- Test: `tests/test_pipeline_loader_file.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pipeline_loader_file.py`:

```python
import pytest

from opsgentic.pipeline.spec import PipelineSpecError, load_spec_file


def test_load_spec_file_valid(tmp_path):
    p = tmp_path / "ok.yaml"
    p.write_text(
        "entrypoint: a\n"
        "nodes:\n  - {id: a, step: rca, agents: []}\n"
        "edges:\n  - {from: a, to: END}\n"
        "agents: {}\n"
    )
    spec = load_spec_file(p)
    assert [n.id for n in spec.nodes] == ["a"]


def test_load_spec_file_missing_raises(tmp_path):
    with pytest.raises(PipelineSpecError):
        load_spec_file(tmp_path / "nope.yaml")


def test_load_spec_file_malformed_yaml_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("nodes: [unclosed\n")
    with pytest.raises(PipelineSpecError):
        load_spec_file(p)


def test_load_spec_file_missing_node_key_raises(tmp_path):
    p = tmp_path / "x.yaml"
    p.write_text("entrypoint: a\nnodes:\n  - {id: a}\nedges: []\nagents: {}\n")
    with pytest.raises(PipelineSpecError):  # missing 'step'
        load_spec_file(p)


def test_load_spec_file_missing_edge_key_raises(tmp_path):
    p = tmp_path / "y.yaml"
    p.write_text(
        "entrypoint: a\n"
        "nodes:\n  - {id: a, step: rca, agents: []}\n"
        "edges:\n  - {to: END}\n"      # missing 'from'
        "agents: {}\n"
    )
    with pytest.raises(PipelineSpecError):
        load_spec_file(p)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_pipeline_loader_file.py -v`
Expected: FAIL — `ImportError: cannot import name 'load_spec_file'`.

- [ ] **Step 3: Harden `parse_spec` and add `load_spec_file`**

In `src/opsgentic/pipeline/spec.py`:

(a) In `parse_spec`, BEFORE the `nodes = tuple(...)` comprehension, add required-key checks so a missing `id`/`step`/`from` raises `PipelineSpecError` (not a raw `KeyError`):

```python
    raw_nodes = raw.get("nodes") or []
    for n in raw_nodes:
        if not isinstance(n, dict) or "id" not in n or "step" not in n:
            raise PipelineSpecError(f"node entry must have 'id' and 'step': {n!r}")
    raw_edges = raw.get("edges") or []
    for e in raw_edges:
        if not isinstance(e, dict) or "from" not in e:
            raise PipelineSpecError(f"edge entry must have 'from': {e!r}")
```

Then change the two comprehensions to iterate `raw_nodes` / `raw_edges` (instead of `raw.get("nodes") or []` / `raw.get("edges") or []`) so they reuse the validated lists:

```python
    nodes = tuple(
        NodeSpec(
            id=str(n["id"]),
            step=str(n["step"]),
            agents=tuple(str(a) for a in (n.get("agents") or [])),
        )
        for n in raw_nodes
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
        for e in raw_edges
    )
```

(b) Add `load_spec_file` and make `load_spec` delegate to it. Replace the existing `@lru_cache load_spec()` body:

```python
def load_spec_file(path: str | Path) -> PipelineSpec:
    """Parse and validate a specific spec file (uncached). Wraps a missing file and
    malformed YAML into PipelineSpecError so callers get a single error type."""
    p = Path(path)
    if not p.exists():
        raise PipelineSpecError(f"pipeline spec not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text())
    except yaml.YAMLError as exc:
        raise PipelineSpecError(f"invalid YAML in {p}: {exc}") from None
    return parse_spec(raw or {})


@lru_cache
def load_spec() -> PipelineSpec:
    """Load and validate the configured pipeline spec once (cached). Edits require a
    process restart, consistent with the agent-skill loader."""
    return load_spec_file(_resolve_path())
```

Keep the existing `_resolve_path()` unchanged. `Path` is already imported.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_pipeline_loader_file.py tests/test_pipeline_spec.py -v`
Expected: all PASS (5 new + 7 existing). The existing `test_pipeline_spec.py` must still pass unchanged.

- [ ] **Step 5: Full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all green.

- [ ] **Step 6: Checkpoint (stage + suggest commit — user runs it)**

Print for the user:
```bash
git add src/opsgentic/pipeline/spec.py tests/test_pipeline_loader_file.py
git commit -m "feat(pipeline): load_spec_file + clear errors for malformed specs"
```

---

### Task 2: `render_pipeline` formatter

**Files:**
- Create: `src/opsgentic/pipeline/render.py`
- Test: `tests/test_pipeline_render.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_pipeline_render.py`:

```python
from opsgentic.pipeline.render import render_pipeline
from opsgentic.pipeline.spec import load_spec


def test_render_pipeline_contains_topology():
    load_spec.cache_clear()
    out = render_pipeline(load_spec())
    assert "entrypoint=rca" in out
    assert "interrupt_before=[action]" in out
    for node in ("rca", "resolve_target", "validation", "action"):
        assert node in out
    assert "after_validation" in out
    assert "escalate->END" in out
    assert "kubernetes" in out
    assert "pr-responder" in out


def test_render_pipeline_is_deterministic():
    load_spec.cache_clear()
    spec = load_spec()
    assert render_pipeline(spec) == render_pipeline(spec)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_pipeline_render.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'opsgentic.pipeline.render'`.

- [ ] **Step 3: Implement `render.py`**

Create `src/opsgentic/pipeline/render.py`:

```python
from __future__ import annotations

from opsgentic.pipeline.spec import PipelineSpec


def _tools_line(name: str, tools, width: int) -> str:
    joined = ", ".join(sorted(tools)) or "(none)"
    return f"  {name.ljust(width)}  {joined}"


def render_pipeline(spec: PipelineSpec) -> str:
    """Human-readable summary of a pipeline spec: nodes, edges (with routing), and
    per-agent tool wiring. Deterministic ordering (spec declaration order)."""
    lines: list[str] = []
    interrupts = ", ".join(spec.interrupt_before) or "(none)"
    lines.append(f"Pipeline: entrypoint={spec.entrypoint}  interrupt_before=[{interrupts}]")

    node_w = max((len(n.id) for n in spec.nodes), default=0)
    lines += ["", "Nodes:"]
    for n in spec.nodes:
        agents = ", ".join(n.agents) or "(none)"
        lines.append(f"  {n.id.ljust(node_w)}  step={n.step}  agents=[{agents}]")

    edge_w = max((len(e.source) for e in spec.edges), default=0)
    lines += ["", "Edges:"]
    for e in spec.edges:
        if e.conditional:
            branches = ", ".join(f"{k}->{v}" for k, v in e.branches.items())
            lines.append(f"  {e.source.ljust(edge_w)} -[{e.route}]-> {branches}")
        else:
            lines.append(f"  {e.source.ljust(edge_w)} -> {e.target}")

    if spec.agent_tools:
        agent_w = max(len(a) for a in spec.agent_tools)
        lines += ["", "Agent tools:"]
        for agent, tools in spec.agent_tools.items():
            lines.append(_tools_line(agent, tools, agent_w))

    if spec.off_graph_tools:
        off_w = max(len(a) for a in spec.off_graph_tools)
        lines += ["", "Off-graph agents:"]
        for agent, tools in spec.off_graph_tools.items():
            lines.append(_tools_line(agent, tools, off_w))

    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_pipeline_render.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Checkpoint (stage + suggest commit — user runs it)**

Print for the user:
```bash
git add src/opsgentic/pipeline/render.py tests/test_pipeline_render.py
git commit -m "feat(pipeline): render_pipeline text formatter"
```

---

### Task 3: `opsgentic pipeline` CLI subcommand (non-breaking)

**Files:**
- Modify: `src/opsgentic/cli.py`
- Test: `tests/test_pipeline_cli.py`

The existing `opsgentic --file …` invocation MUST keep working. Dispatch on the first argv token: `pipeline` routes to the new handler; anything else runs the graph (legacy).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pipeline_cli.py`:

```python
from opsgentic.cli import _pipeline_main


def test_pipeline_cli_validate_ok(capsys):
    rc = _pipeline_main(["validate"])
    assert rc == 0
    assert "OK" in capsys.readouterr().out


def test_pipeline_cli_validate_bad(tmp_path, capsys):
    p = tmp_path / "bad.yaml"
    p.write_text(
        "entrypoint: ghost\n"                       # entrypoint not a node
        "nodes:\n  - {id: a, step: rca, agents: []}\n"
        "edges: []\n"
        "agents: {}\n"
    )
    rc = _pipeline_main(["validate", str(p)])
    assert rc == 1
    assert "INVALID" in capsys.readouterr().out


def test_pipeline_cli_show(capsys):
    rc = _pipeline_main(["show"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "entrypoint=rca" in out
    assert "after_validation" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_pipeline_cli.py -v`
Expected: FAIL — `ImportError: cannot import name '_pipeline_main'`.

- [ ] **Step 3: Rewrite `cli.py`**

Replace the entire contents of `src/opsgentic/cli.py` with:

```python
from __future__ import annotations

import argparse
import json
import logging
import sys

from opsgentic import runner
from opsgentic.config import get_settings
from opsgentic.triggers import normalize


def _run_main(argv: list[str]) -> None:
    """Legacy entry: run the graph synchronously from a trigger payload file."""
    parser = argparse.ArgumentParser(description="Run an opsgentic graph locally.")
    parser.add_argument("--file", required=True, help="Path to alert/chat JSON")
    parser.add_argument("--source", choices=["grafana", "chat"], default="grafana")
    parser.add_argument("--approve", action="store_true", help="Auto-approve remediation")
    args = parser.parse_args(argv)

    with open(args.file) as f:
        payload = json.load(f)

    normalizer = normalize.from_grafana if args.source == "grafana" else normalize.from_chat
    result = runner.execute_run(normalizer(payload))   # CLI runs the graph synchronously
    print(json.dumps(result, indent=2, default=str))

    if result["awaiting_approval"] and args.approve:
        print("\n--- approving ---\n")
        result = runner.execute_approve(result["thread_id"])
        print(json.dumps(result, indent=2, default=str))


def _cmd_validate(path: str) -> int:
    from opsgentic.pipeline.spec import PipelineSpecError, load_spec_file

    try:
        spec = load_spec_file(path)
    except PipelineSpecError as exc:
        print(f"INVALID: {exc}")
        return 1
    print(f"OK: {path} ({len(spec.nodes)} nodes, entrypoint={spec.entrypoint})")
    return 0


def _cmd_show(path: str) -> int:
    from opsgentic.pipeline.render import render_pipeline
    from opsgentic.pipeline.spec import PipelineSpecError, load_spec_file

    try:
        spec = load_spec_file(path)
    except PipelineSpecError as exc:
        print(f"INVALID: {exc}")
        return 1
    print(render_pipeline(spec))
    return 0


def _pipeline_main(argv: list[str]) -> int:
    """`opsgentic pipeline {validate,show} [path]` — inspect/validate the blueprint."""
    parser = argparse.ArgumentParser(
        prog="opsgentic pipeline", description="Inspect and validate the pipeline blueprint."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, help_text in (("validate", "Validate a pipeline spec file"),
                            ("show", "Print the pipeline topology")):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("path", nargs="?", default=None,
                        help="Spec path (default: configured pipeline_config_path)")
    args = parser.parse_args(argv)

    path = args.path or get_settings().pipeline_config_path
    return _cmd_validate(path) if args.cmd == "validate" else _cmd_show(path)


def main() -> None:
    logging.basicConfig(level=getattr(logging, get_settings().log_level.upper(), logging.INFO))
    argv = sys.argv[1:]
    if argv and argv[0] == "pipeline":
        raise SystemExit(_pipeline_main(argv[1:]))
    _run_main(argv)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the CLI tests**

Run: `.venv/bin/python -m pytest tests/test_pipeline_cli.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Verify the legacy invocation still works (no regression)**

Run: `env LLM_BASE_URL= LLM_API_KEY= MCP_ENABLED=false DATABASE_URL= .venv/bin/opsgentic --file examples/grafana_alert.json --source grafana 2>&1 | tail -3`
Expected: prints the run snapshot JSON ending near `"execution_status": "awaiting_approval"` — the legacy path is unaffected.

Then verify the new subcommands from the shell:
Run: `.venv/bin/opsgentic pipeline show`
Expected: prints the topology (Nodes / Edges / Agent tools sections).
Run: `.venv/bin/opsgentic pipeline validate; echo "exit=$?"`
Expected: prints `OK: config/pipeline.yaml (4 nodes, entrypoint=rca)` and `exit=0`.

- [ ] **Step 6: Full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all green.

- [ ] **Step 7: Checkpoint (stage + suggest commit — user runs it)**

Print for the user:
```bash
git add src/opsgentic/cli.py tests/test_pipeline_cli.py
git commit -m "feat(cli): opsgentic pipeline validate/show (non-breaking subcommand)"
```

---

### Task 4: Blueprint docs — CLI usage + "add a node"

**Files:**
- Modify: `README.md` (the "🧭 Editing the pipeline (blueprint)" section)

- [ ] **Step 1: Add CLI usage + worked example**

In `README.md`, inside the "🧭 Editing the pipeline (blueprint)" section, immediately AFTER the bullet list (the bullet that begins "**One source of truth**") and BEFORE the line starting "Agent *behavior* (prompts) is tuned separately", insert:

````markdown
Inspect and validate the blueprint from the CLI:

```bash
opsgentic pipeline show                  # print the active topology
opsgentic pipeline validate              # validate config/pipeline.yaml (exit 1 on error)
opsgentic pipeline validate my.yaml      # validate a specific file
```

**Example — add a `notify` node after `action`:**

1. Register the step in [`src/opsgentic/pipeline/registry.py`](src/opsgentic/pipeline/registry.py):
   ```python
   def notify_node(state: MachineState) -> dict:
       ...                                   # your logic; returns partial state
   STEP_REGISTRY = {..., "notify": notify_node}
   ```
2. Wire it in `config/pipeline.yaml`:
   ```yaml
   nodes:
     - id: notify
       step: notify
       agents: []
   edges:
     - from: action
       to: notify          # replaces: action -> END
     - from: notify
       to: END
   ```
3. Run `opsgentic pipeline validate`, then restart. `opsgentic pipeline show` reflects the new graph.
````

- [ ] **Step 2: Verify the section reads correctly**

Run: `grep -n "opsgentic pipeline show\|add a \`notify\` node\|pipeline validate" README.md`
Expected: matches inside the blueprint section.

- [ ] **Step 3: Checkpoint (stage + suggest commit — user runs it)**

Print for the user:
```bash
git add README.md
git commit -m "docs: pipeline CLI usage and add-a-node example"
```

---

### Task 5: Full regression + live verification + changelog

**Files:** `.claude/changelogs/2026-07-08.md`

- [ ] **Step 1: Run the whole suite**

Run: `.venv/bin/python -m pytest -v`
Expected: all PASS (Phase 1 tests + the three new Phase 2 files). Capture the count.

- [ ] **Step 2: End-to-end CLI smoke of all three commands**

Run each and confirm behavior:
- `.venv/bin/opsgentic pipeline validate` → `OK: config/pipeline.yaml (4 nodes, entrypoint=rca)`, exit 0.
- `.venv/bin/opsgentic pipeline show` → topology text.
- `env LLM_BASE_URL= MCP_ENABLED=false DATABASE_URL= .venv/bin/opsgentic --file examples/grafana_alert.json --source grafana | tail -1` → legacy run still prints JSON.
- Negative: `printf 'entrypoint: ghost\nnodes:\n  - {id: a, step: rca, agents: []}\nedges: []\nagents: {}\n' > /tmp/bad.yaml && .venv/bin/opsgentic pipeline validate /tmp/bad.yaml; echo "exit=$?"` → prints `INVALID: entrypoint 'ghost' is not a declared node` and `exit=1`.

- [ ] **Step 3: Write the session changelog**

Append to `.claude/changelogs/2026-07-08.md` (create if absent):

```markdown
# 2026-07-08

## Pipeline blueprint — Phase 2 (CLI tooling + docs)

- Converted `config/pipeline.yaml` to block-style YAML (readability); topology unchanged.
- `pipeline/spec.py`: added uncached `load_spec_file(path)` and wrapped missing-file /
  malformed-YAML / missing-key cases into `PipelineSpecError`; `load_spec()` now delegates to it.
- Added `pipeline/render.py` (`render_pipeline`) — human-readable topology summary.
- `cli.py`: added a non-breaking `opsgentic pipeline {validate,show} [path]` subcommand;
  `opsgentic --file ...` unchanged.
- Docs: README blueprint section now covers the CLI and an "add a node" example.
- Tests: added `test_pipeline_loader_file.py`, `test_pipeline_render.py`, `test_pipeline_cli.py`.
```

- [ ] **Step 4: Checkpoint (stage + suggest commit — user runs it)**

Print for the user:
```bash
git add .claude/changelogs/2026-07-08.md docs/superpowers/plans/2026-07-08-pipeline-cli-phase2.md
git commit -m "docs(changelog): pipeline phase 2"
```

---

## Out of scope (later)

- Moving the self-heal loop cap (`MAX_RCA_ATTEMPTS`) and node labels into the spec.
- ConfigMap delivery/override of `pipeline.yaml` in-cluster.
- Console graph editor / drag-drop (Phase 3).
- A JSON Schema for `pipeline.yaml` (IDE autocomplete) — deferred per user.

## Self-review notes

- **Coverage:** validate (Task 1+3), show (Task 2+3), clear errors (Task 1), docs+example (Task 4), regression+live verify (Task 5).
- **Non-breaking:** `main()` dispatches on `argv[0] == "pipeline"`; the legacy flat parser runs otherwise. Verified live in Task 3 Step 5 and Task 5 Step 2.
- **Types:** `load_spec_file` returns `PipelineSpec`; CLI handlers return `int` exit codes; `main()` raises `SystemExit(code)` for the pipeline path.
- **No placeholders:** every step has full code/commands and expected output.
