# Console Graph Visualize — Design

## Goal

Add a graph visualization to opsgentic-console that shows the agent system as a
graph: **agents**, the **MCP tools** they use, **skills** wired into them, the
control-flow **edges** between them, and **memory** (the run-state checkpoint).

Two views:

1. **System graph (overall)** — the static topology of the whole system, derived
   from the real code/config so it never drifts from what actually runs.
2. **Run graph (per run)** — the actual execution trace of a single run: which
   nodes ran, in what order (including the RCA self-heal loop), which tools were
   actually called, and the status/output of each step.

## Scope

- Two read-only views inside the existing console (new **Graph** tab).
- System graph: agents, graph nodes, MCP servers (+ lazy tool expand), skills,
  one memory node, and edges between them.
- Run graph: real execution trace from the LangGraph checkpoint history plus the
  recorded tool-call audit trail.
- Reuses the existing console patterns: Node BFF proxy + single static
  `index.html` (vanilla JS, no build step), 5s polling for the run view.

## Decisions (confirmed during brainstorming)

- **Single source of truth = Python API.** A new `opsgentic/graphview.py` builds
  the graph JSON by introspecting the compiled LangGraph (`app.get_graph()`),
  the agent-skill wiring (`agent_skills._load_all()`), and the MCP config
  (`mcp/loader.load_connections()`). The console only proxies and renders.
- **Run trace = checkpoint history.** Built from `app.get_state_history(config)`
  so the executed node path (and rca loops) is accurate, plus the tool-call
  audit trail already stored in `context_data.tool_calls`.
- **Memory = the checkpoint store** (LangGraph Postgres/Memory saver, per-run
  state + messages). Rendered as a single `Memory (checkpoint)` node.
- **MCP tools: server-level by default, lazy expand to real tools.** The graph
  shows the 3 servers; clicking a server fetches the real tool names on demand
  (reusing the `mcp.diagnose` probe path). This stays correct even when a server
  is not deployed.
- **Render with Cytoscape.js, vendored.** A single UMD file under
  `console/public/vendor/`, served by the existing `express.static`. Layout:
  built-in `breadthfirst` (layered) for the DAG. No build step, works offline
  in-cluster.

## Agent / tool / skill mapping (ground truth)

Verified from the code and `deploy/manifests/agent-skills/*.md` frontmatter:

| Agent          | Graph node        | MCP tools (include)              | Skills                       |
|----------------|-------------------|----------------------------------|------------------------------|
| `context`      | `rca` (gather)    | kubernetes, prometheus           | sre                          |
| `rca`          | `rca`             | — (reasons over `context_data`)  | sre                          |
| `resolver`     | `resolve_target`  | — (LLM picks from candidates)    | gitops                       |
| `validation`   | `validation`      | — (deterministic skill registry) | validation                   |
| `remediation`  | `action`          | kubernetes, github, prometheus   | code, gitops, sre, validation|
| `pr-responder` | (webhook, off-DAG)| kubernetes, prometheus, github   | sre, pr-responder            |

Control flow (from `app.get_graph()`):
`START → rca → resolve_target → validation → {action | rca (self-heal) | END}`,
`action → END`. The `validation → {…}` edges are conditional.

To prevent the agent→tool `include` sets from drifting between the graph and the
runtime, the literal sets currently inline in `mcp/context.py`,
`gitops/remediator.py`, and `conversation/responder.py` are extracted into named
constants in one place and imported by both the runtime call sites and
`graphview.py`.

## Architecture

### Backend — `opsgentic/graphview.py` (new) + endpoints in `main.py`

Shared JSON schema:

```json
{
  "nodes": [{ "id": "...", "type": "agent|node|server|skill|memory",
              "label": "...", "group": "..." }],
  "edges": [{ "source": "...", "target": "...",
              "kind": "flow|uses-tool|has-skill|writes-memory",
              "conditional": false }]
}
```

**`GET /graph`** — system topology:

- Read control-flow nodes/edges from `app.get_graph()` (preserve the
  `conditional` flag on the validation edges).
- For each graph node, attach its agent(s) and, per the mapping table:
  - `uses-tool` edges to the relevant MCP **server** nodes.
  - `has-skill` edges from the skills returned by `agent_skills._load_all()`
    (filter by the skill's `agents:` field).
  - a `writes-memory` edge to the single `Memory (checkpoint)` node.
- Include `pr-responder` and its tools/skills as a separate `group` (webhook),
  not connected to the main DAG flow edges.
- Server nodes carry their configured transport/url (from `load_connections()`)
  and a `tools: null` placeholder (populated lazily by the endpoint below).

**`GET /graph/tools/{server}`** — lazy tool list for one MCP server:

- Reuses the `mcp.diagnose` probe to connect and list real tool names.
- Returns `{ "server": "...", "tools": ["..."], "error": "..."|null }`.
- Errors (server not deployed) return `200` with `tools: []` and an `error`
  string so the UI can show "unavailable" rather than failing the whole graph.

**`GET /runs/{id}/graph`** — run trace:

- Walk `app.get_state_history(config)` (oldest→newest) to reconstruct the
  ordered list of executed nodes, with a step index and rca loop iteration.
- Overlay onto the same node/edge schema:
  - per-node `status`: `ran | current | pending`.
  - `executed`: ordered `[{ step, node, iteration }]` describing the active path.
  - `tool_calls`: from the final state `context_data.tool_calls` (and
    `summary.tool_calls` for `prcomment-*` runs), grouped by the node/agent that
    made them, with a per-node count for the badge.
  - run summary fields already available: `hypothesis`, `remediation_plan`,
    `pr_url`, `execution_status`.
- For `prcomment-*` runs (no graph checkpoint), return a minimal pr-responder
  trace built from the stored `summary` only.

### Console BFF — `console/server.js`

Add three proxy routes (same pattern as the existing `/api/runs*`):

- `GET /api/graph` → API `GET /graph`
- `GET /api/graph/tools/:server` → API `GET /graph/tools/:server`
- `GET /api/runs/:id/graph` → API `GET /runs/:id/graph`

### Frontend — `console/public/index.html` (+ `vendor/cytoscape.min.js`)

- New **Graph** nav button and `#tab-graph` section.
- A `System | Run` sub-toggle inside the tab. Run mode shows a run picker
  (populated from `/api/runs`) or is opened via a new "Graph" action on a row in
  the Runs tab.
- Cytoscape graph with `breadthfirst` layout.
- Node styling by `type`: agent (rectangle), server (rounded, colored by reusing
  the existing `grp-*` server colors), skill (tag/chip), memory (distinct
  cylinder-like style). `conditional` flow edges rendered dashed.
- **Lazy tool expand:** clicking a server node calls `/api/graph/tools/:server`
  and adds tool child nodes (or shows an "unavailable" note).
- **Run overlay:** executed nodes highlighted, not-run nodes dimmed, a tool-call
  count badge per node; clicking a node opens a detail panel (reuse the existing
  modal) showing that node's tool calls / hypothesis / plan as applicable.
- Run view auto-refreshes on the existing 5s cadence while the run is active.

## Testing

- Python unit tests (new `tests/` dir, pytest):
  - system graph builder: asserts the expected node types, the agent→tool /
    agent→skill / agent→memory edges, and that the conditional validation edges
    are present and flagged.
  - run trace builder: feed a synthesized `get_state_history()` (including an
    rca self-heal loop) and assert the executed step order, loop iteration, and
    tool-call grouping.
- Manual: open the console, confirm the System graph matches the mapping table,
  expand a server's tools, then open a real run's graph and confirm the path and
  tool calls match the existing run detail.

## Out of scope (YAGNI)

- Live websocket/streaming updates (the run view uses the existing 5s polling).
- Editing topology or config from the graph (read-only views).
- Tool-call latency/timing metrics on edges.
- Non-GitHub provider-specific topology nuances beyond the existing server set.
