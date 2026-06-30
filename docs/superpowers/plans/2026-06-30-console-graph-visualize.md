# Console Graph Visualize Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Repo rule:** This repo forbids autonomous `git commit`. Each task ends with a
> staged commit command — review the diff and run it yourself (or confirm before
> a worker runs it). Do not auto-commit without that confirmation.

**Goal:** Add two read-only graph views to opsgentic-console — a System topology graph (agents, MCP tools, skills, memory, control-flow edges) and a per-run execution-trace graph — driven by the Python API as the single source of truth.

**Architecture:** A new `opsgentic/graphview.py` builds normalized `{nodes, edges}` JSON by introspecting the compiled LangGraph (`app.get_graph()`), the agent→tool registry, the agent-skill wiring, and the MCP server config. Per-run traces come from `app.get_state_history()` plus the recorded `context_data.tool_calls`. Three FastAPI endpoints expose this; the Node console proxies them and renders with a vendored Cytoscape.js in a new Graph tab.

**Tech Stack:** Python (FastAPI, LangGraph), pytest, Node/Express (console BFF), vanilla JS + Cytoscape.js (vendored, no build step).

---

## File Structure

- Create: `src/opsgentic/agent_registry.py` — single source of the agent→MCP-server and agent→graph-node mappings (extracted from inline literals so the graph never drifts from runtime).
- Modify: `src/opsgentic/mcp/context.py` — use the registry for the context agent's include set.
- Modify: `src/opsgentic/conversation/responder.py` — use the registry for the pr-responder include set.
- Create: `src/opsgentic/graphview.py` — `build_system_graph()`, `build_run_graph()`, `executed_steps()`, `list_server_tools()`.
- Modify: `src/opsgentic/main.py` — add `GET /graph`, `GET /graph/tools/{server}`, `GET /runs/{thread_id}/graph`.
- Create: `tests/test_agent_registry.py`, `tests/test_graphview.py`.
- Modify: `console/server.js` — proxy the three new endpoints.
- Create: `console/public/vendor/cytoscape.min.js` — vendored library.
- Modify: `console/public/index.html` — Graph tab (nav, section, CSS, JS).
- Modify: `pyproject.toml` — add pytest as a dev/optional dependency.
- Create: `.claude/changelogs/2026-06-30.md` — session changelog (repo rule).

---

## Task 1: Agent registry + refactor inline include sets

**Files:**
- Create: `src/opsgentic/agent_registry.py`
- Modify: `src/opsgentic/mcp/context.py:48`
- Modify: `src/opsgentic/conversation/responder.py:67`
- Test: `tests/test_agent_registry.py`

- [ ] **Step 1: Install pytest into the venv**

Run: `.venv/bin/pip install pytest`
Expected: installs pytest (used by every test task below).

- [ ] **Step 2: Write the failing test**

Create `tests/test_agent_registry.py`:

```python
from opsgentic import agent_registry as reg


def test_agent_tools_mapping_matches_ground_truth():
    assert reg.AGENT_TOOLS["context"] == {"kubernetes", "prometheus"}
    assert reg.AGENT_TOOLS["remediation"] == {"kubernetes", "github", "prometheus"}
    assert reg.AGENT_TOOLS["pr-responder"] == {"kubernetes", "prometheus", "github"}
    # Reasoning-only agents declare no MCP servers.
    assert reg.AGENT_TOOLS["rca"] == set()
    assert reg.AGENT_TOOLS["resolver"] == set()
    assert reg.AGENT_TOOLS["validation"] == set()


def test_node_agents_cover_the_four_graph_nodes():
    assert set(reg.NODE_AGENTS) == {"rca", "resolve_target", "validation", "action"}
    assert reg.NODE_AGENTS["rca"] == ["context", "rca"]
    assert reg.NODE_AGENTS["action"] == ["remediation"]
    assert reg.OFF_GRAPH_AGENTS == ["pr-responder"]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_agent_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'opsgentic.agent_registry'`.

- [ ] **Step 4: Create the registry module**

Create `src/opsgentic/agent_registry.py`:

```python
from __future__ import annotations

# Single source of truth for which MCP servers each logical agent loads tools from,
# and how the logical agents map onto the LangGraph nodes. The runtime call sites
# (mcp/context.py, conversation/responder.py) and the graph view (graphview.py) both
# import these so the visualized topology can never drift from what actually runs.

# Agent -> set of MCP server names it loads read-only tools from.
AGENT_TOOLS: dict[str, set[str]] = {
    "context": {"kubernetes", "prometheus"},
    "rca": set(),            # reasons over context_data; no direct tools
    "resolver": set(),       # LLM picks from precomputed candidates; no direct tools
    "validation": set(),     # deterministic skill registry; no LLM/MCP
    "remediation": {"kubernetes", "github", "prometheus"},
    "pr-responder": {"kubernetes", "prometheus", "github"},
}

# LangGraph node -> the agent(s) that run inside it (order = execution order in the node).
NODE_AGENTS: dict[str, list[str]] = {
    "rca": ["context", "rca"],
    "resolve_target": ["resolver"],
    "validation": ["validation"],
    "action": ["remediation"],
}

# Agents that run outside the alert->remediation DAG (webhook-triggered).
OFF_GRAPH_AGENTS: list[str] = ["pr-responder"]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_agent_registry.py -v`
Expected: PASS (2 passed).

- [ ] **Step 6: Refactor context.py to use the registry**

In `src/opsgentic/mcp/context.py`, add the import near the other `opsgentic` imports (after line 9):

```python
from opsgentic.agent_registry import AGENT_TOOLS
```

Replace line 48:

```python
    tools, tool_server = await load_tools({"kubernetes", "prometheus"}, deny=_CONTEXT_TOOL_DENYLIST)
```

with:

```python
    tools, tool_server = await load_tools(AGENT_TOOLS["context"], deny=_CONTEXT_TOOL_DENYLIST)
```

- [ ] **Step 7: Refactor responder.py to use the registry**

In `src/opsgentic/conversation/responder.py`, add the import after line 11:

```python
from opsgentic.agent_registry import AGENT_TOOLS
```

Replace line 67:

```python
    tools, tool_server = await load_tools({"kubernetes", "prometheus", "github"}, deny=_DENY)
```

with:

```python
    tools, tool_server = await load_tools(AGENT_TOOLS["pr-responder"], deny=_DENY)
```

- [ ] **Step 8: Verify imports still resolve**

Run: `.venv/bin/python -c "import opsgentic.mcp.context, opsgentic.conversation.responder; print('ok')"`
Expected: prints `ok` (no ImportError).

- [ ] **Step 9: Commit**

```bash
git add src/opsgentic/agent_registry.py src/opsgentic/mcp/context.py src/opsgentic/conversation/responder.py tests/test_agent_registry.py pyproject.toml
git commit -m "feat: central agent->tool/node registry for graph view"
```

---

## Task 2: System topology builder

**Files:**
- Create: `src/opsgentic/graphview.py`
- Test: `tests/test_graphview.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_graphview.py`:

```python
from opsgentic import graphview


def _nodes_by_type(graph, t):
    return [n for n in graph["nodes"] if n["type"] == t]


def _edge(graph, kind):
    return [e for e in graph["edges"] if e["kind"] == kind]


def test_system_graph_has_flow_nodes_and_endpoints():
    g = graphview.build_system_graph()
    ids = {n["id"] for n in g["nodes"]}
    assert {"rca", "resolve_target", "validation", "action"} <= ids
    assert {"START", "END", "memory"} <= ids
    # The four flow nodes are typed as agents.
    flow_agents = {n["id"] for n in g["nodes"] if n["type"] == "agent" and n["group"] == "flow"}
    assert {"rca", "resolve_target", "validation", "action"} <= flow_agents


def test_system_graph_conditional_validation_edges():
    g = graphview.build_system_graph()
    flow = _edge(g, "flow")
    # validation fans out to action, rca (self-heal) and END, all conditional.
    cond_targets = {e["target"] for e in flow if e["source"] == "validation" and e["conditional"]}
    assert {"action", "rca", "END"} <= cond_targets
    # The straight edges are not conditional.
    straight = {(e["source"], e["target"]) for e in flow if not e["conditional"]}
    assert ("START", "rca") in straight
    assert ("rca", "resolve_target") in straight


def test_system_graph_tool_skill_memory_edges():
    g = graphview.build_system_graph()
    # action (remediation) uses all three servers.
    uses = {(e["source"], e["target"]) for e in _edge(g, "uses-tool")}
    assert ("action", "server:kubernetes") in uses
    assert ("action", "server:github") in uses
    assert ("action", "server:prometheus") in uses
    # rca node carries the sre skill (wired to context+rca).
    has_skill = {(e["source"], e["target"]) for e in _edge(g, "has-skill")}
    assert ("skill:sre", "rca") in has_skill
    # every flow node writes memory.
    writes = {e["source"] for e in _edge(g, "writes-memory")}
    assert {"rca", "resolve_target", "validation", "action"} <= writes


def test_system_graph_pr_responder_is_off_graph():
    g = graphview.build_system_graph()
    pr = [n for n in g["nodes"] if n["id"] == "pr-responder"]
    assert pr and pr[0]["group"] == "webhook"
    # pr-responder has tool edges but no flow edge in/out.
    flow = _edge(g, "flow")
    assert not any(e["source"] == "pr-responder" or e["target"] == "pr-responder" for e in flow)


def test_servers_present_with_lazy_tools_placeholder():
    g = graphview.build_system_graph()
    servers = {n["id"]: n for n in g["nodes"] if n["type"] == "server"}
    assert "server:kubernetes" in servers
    assert servers["server:kubernetes"]["tools"] is None  # lazily loaded
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_graphview.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'opsgentic.graphview'`.

- [ ] **Step 3: Create graphview.py with the system builder**

Create `src/opsgentic/graphview.py`:

```python
from __future__ import annotations

from opsgentic import agent_skills
from opsgentic.agent_registry import AGENT_TOOLS, NODE_AGENTS, OFF_GRAPH_AGENTS
from opsgentic.mcp.loader import load_connections

# Pretty labels for the four LangGraph nodes.
_NODE_LABELS = {
    "rca": "RCA",
    "resolve_target": "Resolve target",
    "validation": "Validation",
    "action": "Action (remediation)",
}
_ENDPOINT_MAP = {"__start__": "START", "__end__": "END"}


def _compiled_graph():
    """Topology only — the checkpointer choice does not affect get_graph()."""
    from langgraph.checkpoint.memory import MemorySaver

    from opsgentic.graph.builder import build_app

    return build_app(MemorySaver()).get_graph()


def _servers() -> dict:
    return load_connections()


def build_system_graph() -> dict:
    """Static topology: flow nodes (agents), MCP servers, skills, memory, and the
    control-flow / uses-tool / has-skill / writes-memory edges between them."""
    g = _compiled_graph()
    servers = _servers()
    nodes: list[dict] = []
    edges: list[dict] = []
    node_ids: set[str] = set()

    def add_node(node: dict) -> None:
        if node["id"] not in node_ids:
            nodes.append(node)
            node_ids.add(node["id"])

    # Endpoints + flow nodes (each flow node is where an agent runs).
    add_node({"id": "START", "type": "endpoint", "label": "START", "group": "flow"})
    add_node({"id": "END", "type": "endpoint", "label": "END", "group": "flow"})
    for node_id in NODE_AGENTS:
        add_node({"id": node_id, "type": "agent", "label": _NODE_LABELS.get(node_id, node_id),
                  "group": "flow", "agents": NODE_AGENTS[node_id]})

    # Memory + servers.
    add_node({"id": "memory", "type": "memory", "label": "Memory (checkpoint)", "group": "memory"})
    for sname, conn in servers.items():
        add_node({"id": f"server:{sname}", "type": "server", "label": sname, "group": sname,
                  "transport": conn.get("transport"), "url": conn.get("url"), "tools": None})

    # Control-flow edges from the compiled graph.
    for e in g.edges:
        src = _ENDPOINT_MAP.get(e.source, e.source)
        tgt = _ENDPOINT_MAP.get(e.target, e.target)
        edges.append({"source": src, "target": tgt, "kind": "flow",
                      "conditional": bool(getattr(e, "conditional", False))})

    skills = agent_skills._load_all()

    def attach(node_id: str, agents: list[str]) -> None:
        servers_for: set[str] = set().union(*(AGENT_TOOLS.get(a, set()) for a in agents)) if agents else set()
        for s in sorted(servers_for):
            if f"server:{s}" in node_ids:
                edges.append({"source": node_id, "target": f"server:{s}",
                              "kind": "uses-tool", "conditional": False})
        for sk in skills:
            if any(a in sk.agents for a in agents):
                sid = f"skill:{sk.name}"
                add_node({"id": sid, "type": "skill", "label": sk.name, "group": "skill"})
                edges.append({"source": sid, "target": node_id, "kind": "has-skill", "conditional": False})

    for node_id, agents in NODE_AGENTS.items():
        attach(node_id, agents)
        edges.append({"source": node_id, "target": "memory", "kind": "writes-memory", "conditional": False})

    # Off-graph agents (webhook), e.g. pr-responder — tools/skills only, no flow edges.
    for agent in OFF_GRAPH_AGENTS:
        add_node({"id": agent, "type": "agent", "label": agent.replace("-", " ").title(),
                  "group": "webhook", "agents": [agent]})
        attach(agent, [agent])

    return {"nodes": nodes, "edges": edges}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_graphview.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/opsgentic/graphview.py tests/test_graphview.py
git commit -m "feat: system topology graph builder"
```

---

## Task 3: Run-trace builder

**Files:**
- Modify: `src/opsgentic/graphview.py`
- Test: `tests/test_graphview.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_graphview.py`:

```python
class _Snap:
    """Minimal stand-in for a LangGraph StateSnapshot (only .metadata is read)."""
    def __init__(self, metadata):
        self.metadata = metadata


def test_executed_steps_orders_nodes_and_counts_loops():
    # oldest-first history: rca -> resolve -> validation -> rca (loop) -> resolve -> validation
    history = [
        _Snap({"source": "input", "writes": None}),
        _Snap({"source": "loop", "writes": {"rca": {}}}),
        _Snap({"source": "loop", "writes": {"resolve_target": {}}}),
        _Snap({"source": "loop", "writes": {"validation": {}}}),
        _Snap({"source": "loop", "writes": {"rca": {}}}),
        _Snap({"source": "loop", "writes": {"resolve_target": {}}}),
        _Snap({"source": "loop", "writes": {"validation": {}}}),
    ]
    steps = graphview.executed_steps(history)
    assert [s["node"] for s in steps] == [
        "rca", "resolve_target", "validation", "rca", "resolve_target", "validation",
    ]
    assert [s["step"] for s in steps] == [0, 1, 2, 3, 4, 5]
    # rca ran twice -> second occurrence is iteration 2.
    rca_iters = [s["iteration"] for s in steps if s["node"] == "rca"]
    assert rca_iters == [1, 2]


def test_executed_steps_ignores_non_node_writes():
    history = [
        _Snap({"source": "update", "writes": {"execution_status": "approved"}}),
        _Snap({"source": "loop", "writes": {"action": {}}}),
    ]
    steps = graphview.executed_steps(history)
    assert [s["node"] for s in steps] == ["action"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_graphview.py::test_executed_steps_orders_nodes_and_counts_loops -v`
Expected: FAIL — `AttributeError: module 'opsgentic.graphview' has no attribute 'executed_steps'`.

- [ ] **Step 3: Add executed_steps and build_run_graph**

Append to `src/opsgentic/graphview.py`:

```python
_FLOW_NODES = ("rca", "resolve_target", "validation", "action")


def executed_steps(history) -> list[dict]:
    """Reconstruct the ordered list of executed graph nodes from a state history
    (oldest-first iterable of objects exposing `.metadata`). Each loop write that
    names a flow node is one executed step; repeats get an incrementing iteration."""
    steps: list[dict] = []
    counts: dict[str, int] = {}
    for snap in history:
        md = getattr(snap, "metadata", None) or {}
        if md.get("source") != "loop":
            continue
        for node in (md.get("writes") or {}):
            if node in _FLOW_NODES:
                counts[node] = counts.get(node, 0) + 1
                steps.append({"step": len(steps), "node": node, "iteration": counts[node]})
    return steps


def _comment_run_graph(snap: dict) -> dict:
    """Minimal pr-responder trace for a PR-comment run (no graph checkpoint)."""
    s = snap.get("summary") or {}
    servers = _servers()
    nodes = [{"id": "pr-responder", "type": "agent", "label": "PR responder",
              "group": "webhook", "status": "ran"}]
    edges = []
    for srv in sorted(AGENT_TOOLS["pr-responder"]):
        if srv in servers:
            nodes.append({"id": f"server:{srv}", "type": "server", "label": srv,
                          "group": srv, "tools": None})
            edges.append({"source": "pr-responder", "target": f"server:{srv}",
                          "kind": "uses-tool", "conditional": False})
    return {
        "nodes": nodes, "edges": edges,
        "executed": [{"step": 0, "node": "pr-responder", "iteration": 1}],
        "tool_calls": {"pr-responder": s.get("tool_calls") or []},
        "run": {"thread_id": snap.get("thread_id"), "status": snap.get("status"),
                "kind": s.get("kind"), "reply": s.get("reply"), "comment": s.get("comment")},
    }


def build_run_graph(thread_id: str) -> dict:
    """System topology overlaid with one run's actual execution trace."""
    from opsgentic import runner

    snap = runner.get_run(thread_id)
    if (snap.get("summary") or {}).get("source") == "github-comment" or thread_id.startswith("prcomment-"):
        return _comment_run_graph(snap)

    history = list(runner._app.get_state_history(runner._config(thread_id)))
    history.reverse()  # get_state_history yields newest-first
    steps = executed_steps(history)

    graph = build_system_graph()
    ran = {s["node"] for s in steps}
    nxt = set(snap.get("next") or [])
    for n in graph["nodes"]:
        if n.get("group") == "flow" and n["type"] == "agent":
            n["status"] = "current" if n["id"] in nxt else ("ran" if n["id"] in ran else "pending")

    state = snap.get("state") or {}
    tool_calls = (state.get("context_data") or {}).get("tool_calls") or []
    graph["executed"] = steps
    graph["tool_calls"] = {"rca": tool_calls} if tool_calls else {}
    graph["run"] = {
        "thread_id": thread_id,
        "status": snap.get("status"),
        "hypothesis": state.get("hypothesis"),
        "remediation_plan": state.get("remediation_plan"),
        "pr_url": state.get("pr_url"),
        "execution_status": state.get("execution_status"),
        "error": snap.get("error"),
    }
    return graph
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_graphview.py -v`
Expected: PASS (all, including the two new executed_steps tests).

- [ ] **Step 5: Commit**

```bash
git add src/opsgentic/graphview.py tests/test_graphview.py
git commit -m "feat: per-run execution-trace graph builder"
```

---

## Task 4: Lazy tool listing + FastAPI endpoints

**Files:**
- Modify: `src/opsgentic/graphview.py`
- Modify: `src/opsgentic/main.py:111` (near the other `/runs` routes)
- Test: `tests/test_graphview.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_graphview.py`:

```python
def test_list_server_tools_unknown_server_returns_empty():
    out = graphview.list_server_tools("does-not-exist")
    assert out["server"] == "does-not-exist"
    assert out["tools"] == []
    assert out["error"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_graphview.py::test_list_server_tools_unknown_server_returns_empty -v`
Expected: FAIL — `AttributeError: ... has no attribute 'list_server_tools'`.

- [ ] **Step 3: Add list_server_tools**

Append to `src/opsgentic/graphview.py`:

```python
def list_server_tools(server: str) -> dict:
    """Connect to one configured MCP server and return its real tool names. Returns
    an empty list + error string (never raises) so the UI degrades gracefully when a
    server is not deployed."""
    import asyncio

    from opsgentic.mcp.loader import explain_exception

    conns = load_connections(include={server})
    if server not in conns:
        return {"server": server, "tools": [], "error": "not configured"}
    try:
        from opsgentic.mcp.diagnose import _probe

        tools = asyncio.run(_probe(server, conns[server]))
        return {"server": server, "tools": sorted(tools), "error": None}
    except Exception as exc:
        return {"server": server, "tools": [], "error": explain_exception(exc)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_graphview.py::test_list_server_tools_unknown_server_returns_empty -v`
Expected: PASS.

- [ ] **Step 5: Add the three FastAPI endpoints**

In `src/opsgentic/main.py`, insert after the `get_run` route (after line 118):

```python
@app.get("/graph")
def system_graph() -> dict:
    from opsgentic import graphview

    return graphview.build_system_graph()


@app.get("/graph/tools/{server}")
def graph_server_tools(server: str) -> dict:
    from opsgentic import graphview

    return graphview.list_server_tools(server)


@app.get("/runs/{thread_id}/graph")
def run_graph(thread_id: str) -> dict:
    from opsgentic import graphview

    return graphview.build_run_graph(thread_id)
```

- [ ] **Step 6: Verify the app imports and exposes the routes**

Run: `.venv/bin/python -c "from opsgentic.main import app; print(sorted({r.path for r in app.routes if 'graph' in r.path}))"`
Expected: `['/graph', '/graph/tools/{server}', '/runs/{thread_id}/graph']`

- [ ] **Step 7: Run the full Python test suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: PASS (all tests green).

- [ ] **Step 8: Commit**

```bash
git add src/opsgentic/graphview.py src/opsgentic/main.py tests/test_graphview.py
git commit -m "feat: graph API endpoints (system, lazy tools, per-run)"
```

---

## Task 5: Console BFF proxy routes

**Files:**
- Modify: `console/server.js:74` (after the existing `/api/runs` routes)

- [ ] **Step 1: Add the proxy routes**

In `console/server.js`, immediately after the reject route (line 74):

```javascript
app.post('/api/runs/:id/reject', (req, res) => proxy(res, 'POST', `/runs/${encodeURIComponent(req.params.id)}/reject`));

// graph visualize (system topology + per-run trace) -> opsgentic
app.get('/api/graph', (_req, res) => proxy(res, 'GET', '/graph'));
app.get('/api/graph/tools/:server', (req, res) => proxy(res, 'GET', `/graph/tools/${encodeURIComponent(req.params.server)}`));
app.get('/api/runs/:id/graph', (req, res) => proxy(res, 'GET', `/runs/${encodeURIComponent(req.params.id)}/graph`));
```

(Replace the single existing reject line with the block above — the reject line is unchanged, the three graph lines are new.)

- [ ] **Step 2: Verify server.js parses**

Run: `node --check console/server.js`
Expected: no output, exit 0.

- [ ] **Step 3: Commit**

```bash
git add console/server.js
git commit -m "feat: console proxy routes for graph endpoints"
```

---

## Task 6: Vendor Cytoscape.js + Graph tab scaffold

**Files:**
- Create: `console/public/vendor/cytoscape.min.js`
- Modify: `console/public/index.html` (nav, CSS, section)

- [ ] **Step 1: Vendor the Cytoscape.js UMD build**

Run:
```bash
mkdir -p console/public/vendor
curl -fsSL https://cdn.jsdelivr.net/npm/cytoscape@3.30.2/dist/cytoscape.min.js -o console/public/vendor/cytoscape.min.js
test -s console/public/vendor/cytoscape.min.js && head -c 60 console/public/vendor/cytoscape.min.js
```
Expected: file exists, non-empty, starts with the Cytoscape license/banner comment. (If the environment is offline, copy the file from any machine with `npm pack cytoscape@3.30.2`.)

- [ ] **Step 2: Add the Graph nav button**

In `console/public/index.html`, change the nav block (lines 48-53) to add a Graph button after Runs:

```html
  <nav>
    <button data-tab="runs" class="active">Runs</button>
    <button data-tab="graph">Graph</button>
    <button data-tab="chat">Chat</button>
    <button data-tab="skills">Skills</button>
    <button data-tab="config">Config</button>
  </nav>
```

- [ ] **Step 3: Add Graph-tab CSS**

In the `<style>` block, append before the closing `</style>` (after line 42):

```css
  #cy { height:560px; background:#0d1117; border:1px solid var(--line); border-radius:8px; }
  .g-toolbar { display:flex; gap:8px; align-items:center; margin-bottom:10px; flex-wrap:wrap; }
  .g-toolbar .seg button { background:#0d1117; color:var(--mut); border:1px solid var(--line); padding:6px 12px; cursor:pointer; }
  .g-toolbar .seg button.on { background:var(--acc); color:#fff; border-color:var(--acc); }
  .g-toolbar select { width:auto; min-width:240px; }
  .g-legend { display:flex; gap:12px; flex-wrap:wrap; font-size:12px; color:var(--mut); margin-top:8px; }
  .g-legend span b { color:var(--fg); }
```

- [ ] **Step 4: Add the Graph section markup**

In `console/public/index.html`, add a new `<section>` after the runs section (after line 64):

```html
  <section id="tab-graph" class="hide">
    <div class="panel">
      <div class="g-toolbar">
        <div class="seg">
          <button id="g-mode-system" class="on">System</button>
          <button id="g-mode-run">Run</button>
        </div>
        <select id="g-run-sel" class="hide"></select>
        <span class="sp" style="flex:1"></span>
        <button class="act" id="g-fit">Fit</button>
        <span class="mut" id="g-status"></span>
      </div>
      <div id="cy"></div>
      <div class="g-legend">
        <span><b>▭</b> agent</span><span><b>◉</b> MCP server</span>
        <span><b>▰</b> skill</span><span><b>⬡</b> memory</span>
        <span><b>– –</b> conditional edge</span>
      </div>
    </div>
  </section>
```

- [ ] **Step 5: Load the vendored library**

In `console/public/index.html`, add this line immediately before the existing `<script>` (before line 112):

```html
<script src="/vendor/cytoscape.min.js"></script>
```

- [ ] **Step 6: Verify the static assets serve**

Run: `node console/server.js & sleep 1; curl -sS -o /dev/null -w "%{http_code}\n" http://localhost:3000/vendor/cytoscape.min.js; curl -sS -o /dev/null -w "%{http_code}\n" http://localhost:3000/; kill %1`
Expected: two `200` lines. (Server.js reads a SA token on K8s API calls only; static serving works locally without it.)

- [ ] **Step 7: Commit**

```bash
git add console/public/vendor/cytoscape.min.js console/public/index.html
git commit -m "feat: vendor cytoscape + graph tab scaffold"
```

---

## Task 7: Graph tab rendering (System view + lazy tools)

**Files:**
- Modify: `console/public/index.html` (JS — tab wiring + render functions)

- [ ] **Step 1: Wire the Graph tab into the tab switcher**

In `console/public/index.html`, replace the tab-list line (line 133):

```javascript
  ['chat','runs','skills','config'].forEach(t=>$('#tab-'+t).classList.toggle('hide', t!==b.dataset.tab));
```

with:

```javascript
  ['chat','runs','skills','config','graph'].forEach(t=>$('#tab-'+t).classList.toggle('hide', t!==b.dataset.tab));
```

and add this line inside the same handler, after the `if(b.dataset.tab==='config') loadConfig();` line (line 137):

```javascript
  if(b.dataset.tab==='graph') openGraphTab();
```

- [ ] **Step 2: Add the Cytoscape style + element mapping helpers**

Before the final `</script>` (line 260), add:

```javascript
// ---- graph visualize ----
let CY=null, GMODE='system', GRUN=null;
const SERVER_COLORS={kubernetes:'#1f6feb',prometheus:'#e6522c',github:'#6e40c9'};
function cyStyle(){ return [
  { selector:'node', style:{ 'label':'data(label)','color':'#e6edf3','font-size':11,
      'text-valign':'center','text-halign':'center','text-wrap':'wrap','text-max-width':110 } },
  { selector:'node[type="agent"]', style:{ 'shape':'round-rectangle','background-color':'#1a2230',
      'border-color':'#4c8eff','border-width':2,'width':130,'height':46,'text-valign':'center' } },
  { selector:'node[group="webhook"]', style:{ 'border-color':'#d29922','border-style':'dashed' } },
  { selector:'node[type="endpoint"]', style:{ 'shape':'ellipse','background-color':'#2b3648',
      'border-width':0,'width':54,'height':54,'color':'#8b98a9' } },
  { selector:'node[type="server"]', style:{ 'shape':'round-rectangle','background-color':'data(color)',
      'border-width':0,'width':120,'height':38,'color':'#fff' } },
  { selector:'node[type="skill"]', style:{ 'shape':'round-rectangle','background-color':'#30363d',
      'border-width':0,'width':96,'height':30,'font-size':10,'color':'#c9d1d9' } },
  { selector:'node[type="memory"]', style:{ 'shape':'barrel','background-color':'#16301c',
      'border-color':'#3fb950','border-width':2,'width':120,'height':44,'color':'#3fb950' } },
  { selector:'node[type="tool"]', style:{ 'shape':'round-rectangle','background-color':'#0d1117',
      'border-color':'#2b3648','border-width':1,'width':110,'height':26,'font-size':9,'color':'#8b98a9' } },
  { selector:'edge', style:{ 'width':1.5,'line-color':'#2b3648','target-arrow-color':'#2b3648',
      'target-arrow-shape':'triangle','curve-style':'bezier' } },
  { selector:'edge[kind="flow"]', style:{ 'line-color':'#4c8eff','target-arrow-color':'#4c8eff','width':2 } },
  { selector:'edge[conditional="yes"]', style:{ 'line-style':'dashed' } },
  { selector:'edge[kind="has-skill"]', style:{ 'line-color':'#30363d','target-arrow-shape':'none','line-style':'dotted' } },
  { selector:'edge[kind="writes-memory"]', style:{ 'line-color':'#26402c','target-arrow-color':'#26402c','line-style':'dashed' } },
  { selector:'edge[kind="uses-tool"]', style:{ 'line-color':'#3a3f4b','target-arrow-color':'#3a3f4b' } },
  // run-overlay states
  { selector:'node.ran', style:{ 'border-color':'#3fb950','border-width':3 } },
  { selector:'node.current', style:{ 'border-color':'#d29922','border-width':3 } },
  { selector:'node.dimmed', style:{ 'opacity':0.35 } },
  { selector:'node.badge', style:{ 'label':'data(blabel)' } },
]; }

function graphToElements(g){
  const els=[];
  (g.nodes||[]).forEach(n=>{
    const d={ id:n.id, label:n.label, type:n.type, group:n.group||'' };
    if(n.type==='server') d.color=SERVER_COLORS[n.label]||'#3a3f4b';
    els.push({ data:d });
  });
  (g.edges||[]).forEach(e=>els.push({ data:{
    id:e.source+'->'+e.target+':'+e.kind, source:e.source, target:e.target,
    kind:e.kind, conditional:e.conditional?'yes':'no' } }));
  return els;
}

function renderGraph(g){
  CY = cytoscape({ container:$('#cy'), elements:graphToElements(g), style:cyStyle(),
    layout:{ name:'breadthfirst', directed:true, spacingFactor:1.2, padding:20,
      roots:['START'] }, wheelSensitivity:0.2 });
  CY.on('tap','node[type="server"]', (evt)=>expandServerTools(evt.target));
  CY.on('tap','node', (evt)=>showNodeDetail(evt.target.id()));
  return CY;
}
```

- [ ] **Step 3: Add the System-view loader + lazy tool expand**

Continue adding before `</script>`:

```javascript
async function openGraphTab(){
  if(GMODE==='system') loadSystemGraph(); else loadRunPicker();
}
async function loadSystemGraph(){
  $('#g-status').textContent='loading…';
  try{ const g=await j('GET','/api/graph'); renderGraph(g); $('#g-status').textContent=''; }
  catch(e){ $('#g-status').textContent=e.message; }
}
async function expandServerTools(node){
  const id=node.id(); if(node.data('expanded')) return; node.data('expanded',true);
  const server=node.data('label');
  $('#g-status').textContent='listing '+server+' tools…';
  try{
    const r=await j('GET','/api/graph/tools/'+encodeURIComponent(server));
    if(r.error && !(r.tools||[]).length){ $('#g-status').textContent=server+': '+r.error; return; }
    (r.tools||[]).forEach(t=>{
      const tid='tool:'+server+':'+t;
      CY.add([{ data:{ id:tid, label:t, type:'tool', group:server } },
              { data:{ id:tid+'-e', source:id, target:tid, kind:'uses-tool', conditional:'no' } }]);
    });
    CY.layout({ name:'breadthfirst', directed:true, spacingFactor:1.2, padding:20, roots:['START'] }).run();
    $('#g-status').textContent='';
  }catch(e){ $('#g-status').textContent=e.message; }
}
function showNodeDetail(id){
  const n=CY.getElementById(id); if(!n||!n.length) return;
  const d=n.data(); const tc=(GRUN&&GRUN.tool_calls&&GRUN.tool_calls[id])||[];
  let body='<div class="panel"><div><b>'+esc(d.label)+'</b> <span class="grp">'+esc(d.type)+'</span></div>';
  if(GRUN && GRUN.run && (id==='rca')) body+=(GRUN.run.hypothesis?'<label>Hypothesis</label><pre>'+esc(GRUN.run.hypothesis)+'</pre>':'');
  if(GRUN && GRUN.run && (id==='action')){ const p=GRUN.run.remediation_plan||{};
    if(p.summary) body+='<label>Plan</label><div class="mut">'+esc(p.summary)+' — <code>'+esc(p.file_path||'')+'</code></div>'; }
  body+=toolCallsHtml(tc)+'</div>';
  $('#modal-body').innerHTML=body; $('#modal').classList.remove('hide');
}
```

- [ ] **Step 4: Add the System/Run toggle + Fit handlers**

Continue adding before `</script>`:

```javascript
$('#g-fit').onclick=()=>{ if(CY) CY.fit(undefined,30); };
$('#g-mode-system').onclick=()=>{ GMODE='system'; GRUN=null;
  $('#g-mode-system').classList.add('on'); $('#g-mode-run').classList.remove('on');
  $('#g-run-sel').classList.add('hide'); loadSystemGraph(); };
$('#g-mode-run').onclick=()=>{ GMODE='run';
  $('#g-mode-run').classList.add('on'); $('#g-mode-system').classList.remove('on');
  $('#g-run-sel').classList.remove('hide'); loadRunPicker(); };
```

- [ ] **Step 5: Manual check — System view renders**

Run the console against a running opsgentic API (or with the API reachable via `OPSGENTIC_API_URL`), open `http://localhost:3000`, click **Graph**.
Expected: a DAG from START → rca → resolve_target → validation → action/END renders; validation's branch edges are dashed; three server nodes are colored; the `sre` skill connects to multiple agents; a `Memory (checkpoint)` node is linked from every flow node; `PR responder` sits apart (webhook). Clicking a server node adds its real tool nodes (or shows an "unavailable" status if the server isn't deployed).

- [ ] **Step 6: Commit**

```bash
git add console/public/index.html
git commit -m "feat: graph tab system view + lazy MCP tool expand"
```

---

## Task 8: Run-view overlay + run picker

**Files:**
- Modify: `console/public/index.html` (JS — run picker + overlay)

- [ ] **Step 1: Add the run picker and run-graph loader**

Before `</script>`, add:

```javascript
let gRunTimer=null;   // GRUN is declared with the other graph globals in Task 7
async function loadRunPicker(){
  try{
    const rows=await j('GET','/api/runs'); const sel=$('#g-run-sel');
    sel.innerHTML='<option value="">— pick a run —</option>'+rows.map(r=>{
      const s=r.summary||{}; const lbl=(s.title||r.thread_id)+' · '+r.status;
      return '<option value="'+esc(r.thread_id)+'">'+esc(lbl)+'</option>'; }).join('');
    sel.onchange=()=>{ if(sel.value) loadRunGraph(sel.value); };
    if(rows.length){ sel.value=rows[0].thread_id; loadRunGraph(rows[0].thread_id); }
  }catch(e){ $('#g-status').textContent=e.message; }
}
function applyRunOverlay(g){
  renderGraph(g);
  const ran=new Set((g.executed||[]).map(s=>s.node));
  CY.nodes().forEach(n=>{
    const id=n.id(), d=n.data();
    if(d.type==='agent' && d.group==='flow'){
      const st = (g.nodes.find(x=>x.id===id)||{}).status;
      if(st==='ran') n.addClass('ran'); else if(st==='current') n.addClass('current');
      else if(st==='pending') n.addClass('dimmed');
    }
    const tc=(g.tool_calls||{})[id]||[];
    if(tc.length){ n.data('blabel', d.label+'  ['+tc.length+' tool]'); n.addClass('badge'); }
  });
}
async function loadRunGraph(id){
  $('#g-status').textContent='loading run…';
  try{
    const g=await j('GET','/api/runs/'+encodeURIComponent(id)+'/graph');
    GRUN=g; applyRunOverlay(g);
    $('#g-status').textContent=(g.run&&g.run.status?('status: '+g.run.status):'');
    clearInterval(gRunTimer);
    if(g.run && !['applied','failed','escalated','rejected','completed'].includes(g.run.status)){
      gRunTimer=setInterval(()=>{ if(GMODE==='run') loadRunGraph(id); }, 5000);
    }
  }catch(e){ $('#g-status').textContent=e.message; }
}
```

- [ ] **Step 2: Stop run polling when leaving the tab**

In the tab switcher handler (the `document.querySelectorAll('nav button').forEach(...)` block), add after `stopRunsAuto();`:

```javascript
  clearInterval(gRunTimer);
```

- [ ] **Step 3: Manual check — Run view overlay**

With at least one real run present, open **Graph → Run**, pick a run.
Expected: executed nodes get a green border, a node awaiting approval gets an amber border, not-yet-run nodes are dimmed; the rca node shows a `[N tool]` badge when the context agent made tool calls; clicking rca opens the hypothesis + tool-call detail, clicking action opens the plan. A PR-comment run shows a single `PR responder` node with its reply/tool calls. An active run refreshes every 5s.

- [ ] **Step 4: Commit**

```bash
git add console/public/index.html
git commit -m "feat: graph tab per-run trace overlay + run picker"
```

---

## Task 9: Changelog + full verification

**Files:**
- Create: `.claude/changelogs/2026-06-30.md`

- [ ] **Step 1: Run the full Python suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: all tests PASS.

- [ ] **Step 2: Lint the JS/Node files**

Run: `node --check console/server.js`
Expected: exit 0.

- [ ] **Step 3: Write the session changelog**

Create `.claude/changelogs/2026-06-30.md`:

```markdown
# 2026-06-30

## Console Graph Visualize

- Added `opsgentic/agent_registry.py` centralizing agent→MCP-server and
  agent→graph-node mappings; refactored `mcp/context.py` and
  `conversation/responder.py` to consume it.
- Added `opsgentic/graphview.py`: `build_system_graph()` (static topology via
  `app.get_graph()` + registry + skills + MCP servers), `build_run_graph()` and
  `executed_steps()` (per-run trace from `get_state_history()` +
  `context_data.tool_calls`), and `list_server_tools()` (lazy MCP tool probe).
- New API endpoints: `GET /graph`, `GET /graph/tools/{server}`,
  `GET /runs/{thread_id}/graph`; proxied by the console BFF.
- Console: new Graph tab (System + Run modes) rendering with vendored
  Cytoscape.js — agents/servers/skills/memory nodes, conditional flow edges,
  lazy tool expansion, and run-trace overlay (ran/current/pending + tool badges).
- Added `tests/` (pytest) covering the registry and both graph builders.
```

- [ ] **Step 4: Commit**

```bash
git add .claude/changelogs/2026-06-30.md
git commit -m "docs: changelog for console graph visualize"
```
