<p align="center" style="width: fit-content; margin: 0 auto; background: white;">
  <img src="docs/figures/opsgentic-logo.png" alt="logo" width="120"/>
</p>

# OpsGentic

A multi-agent system for automated **Root Cause Analysis (RCA)**, **Validation**, and **Remediation** on Kubernetes. Agents are orchestrated as a stateful graph with [LangGraph](https://github.com/langchain-ai/langgraph); remediation is applied through **GitOps** with a mandatory **human-in-the-loop** approval gate before any pull request is opened.

The LLM runs on a **local vLLM** (OpenAI-compatible) endpoint, configured entirely through environment variables.

## Architecture

![Overview architecture](docs/figures/overview-architecture.png)

A trigger (a Grafana/Alertmanager alert or a user chat message) enters the orchestrator. The orchestrator runs a stateful graph across three core agents:

- **RCA Agent** — enriches context (via read-only MCP tools) and produces a root cause hypothesis.
- **Validation Agent** — verifies the hypothesis using deterministic Validation Skills, then drafts a remediation plan.
- **Action Agent** — opens a Git commit + pull request so a GitOps controller (ArgoCD/Flux) can apply the change.

Two tool layers feed the agents:

- **MCP Servers (read-only)** — reused open-source MCP servers (e.g. `kubernetes-mcp-server`, telemetry) for querying cluster and observability state. No mutation happens here; writes only occur through GitOps.
- **Skills** — plain-Python business logic (Validation Skills) the agents call directly, independent of the MCP layer.

The Action Agent never opens a PR autonomously: the graph pauses at an `interrupt_before` gate and waits for a human to approve, reject, or comment.

## Components

1. **Core Orchestrator & Agents** (`src/opsgentic/graph`) — LangGraph `StateGraph` managing a typed `MachineState` (`alert_payload`, `context_data`, `hypothesis`, `remediation_plan`, `validation_report`, `execution_status`).
2. **Skills** (`src/opsgentic/skills`) — deterministic validation/business logic modules, decoupled from MCP.
3. **MCP Servers & Tooling** (`mcp-config/`) — configuration and packaging for existing open-source MCP servers in read-only mode. (Wiring lands in M2.)
4. **Deployment Manifests** (`deploy/manifests/`) — Kubernetes YAML / Helm, strict RBAC, and event triggers. (Lands in M2.)

## Machine state & graph flow

```
trigger -> RCA -> Validation -> [interrupt_before] -> Action -> END
                      |                                  ^
                      +-- (fail, capped retries) --------+ (self-heal loop)
```

1. **RCA** reads `alert_payload`, gathers `context_data`, writes `hypothesis`.
2. **Validation** runs the Validation Skills. On pass it drafts a `remediation_plan` and sets `execution_status="awaiting_approval"`. On fail it loops back to RCA, up to `MAX_RCA_ATTEMPTS`, then escalates.
3. The graph **pauses before `action`** (`interrupt_before=["action"]`); state is persisted by the checkpointer keyed on `thread_id`. No PR exists yet.
4. A human approves; the run resumes and the **Action Agent** opens the remediation PR.

## Triggers

Both triggers normalize to the same `alert_payload`:

- **Grafana / Alertmanager webhook** — `POST /webhook/grafana`
- **User chat** (operator provides error context) — `POST /chat`

## Project layout

```
src/opsgentic/
  config.py             # Settings (env-driven)
  agents/llm.py         # ChatOpenAI factory pointing at vLLM
  graph/
    state.py            # MachineState (TypedDict)
    builder.py          # StateGraph wiring + interrupt_before
    nodes/              # rca / validation / action
  skills/               # Validation Skills (registry + checks)
  triggers/normalize.py # Grafana + chat -> alert_payload
  gitops/pr.py          # PR creation (stub until M3)
  runner.py             # compiled app + checkpointer + start/approve/reject
  main.py               # FastAPI service
  cli.py                # local runner
mcp-config/             # MCP server config (M2)
deploy/manifests/       # K8s manifests + RBAC (M2)
examples/               # sample grafana_alert.json / chat_input.json
docs/figures/           # diagrams
```

## Quickstart

Requirements: Python 3.11+.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .
cp .env.example .env        # then fill in vLLM / GitOps values
```

If `LLM_BASE_URL` is left empty, the agents fall back to a deterministic canned response, so the graph runs end-to-end before the LLM is configured.

### Run locally (CLI)

```bash
opsgentic --file examples/grafana_alert.json --source grafana --approve
opsgentic --file examples/chat_input.json --source chat
```

### Run the service

```bash
opsgentic-api          # serves on :8080
```

## HTTP API

| Method | Path                          | Description                                   |
| ------ | ----------------------------- | --------------------------------------------- |
| GET    | `/healthz`                    | Health check                                  |
| POST   | `/webhook/grafana`            | Grafana/Alertmanager webhook trigger          |
| POST   | `/chat`                       | User chat trigger (error context in body)     |
| GET    | `/runs/{thread_id}`           | Current run state                             |
| POST   | `/runs/{thread_id}/approve`   | Approve the plan and open the PR              |
| POST   | `/runs/{thread_id}/reject`    | Reject the plan                               |

A trigger returns a `thread_id` and `awaiting_approval`; the operator then calls `approve` or `reject`.

## Configuration

| Variable           | Default       | Description                                                |
| ------------------ | ------------- | ---------------------------------------------------------- |
| `LLM_BASE_URL`     | _(empty)_     | vLLM OpenAI-compatible endpoint. Empty -> canned fallback. |
| `LLM_API_KEY`      | _(empty)_     | vLLM API key (stored in a K8s Secret when deployed).       |
| `LLM_MODEL`        | `local-model` | Model name served by vLLM.                                 |
| `LLM_TEMPERATURE`  | `0.0`         | Sampling temperature.                                      |
| `LLM_MAX_TOKENS`   | `4096`        | Max output tokens.                                         |
| `GIT_PROVIDER`     | `github`      | `github` or `gitlab`.                                      |
| `GIT_TOKEN`        | _(empty)_     | Token for opening PRs (K8s Secret when deployed).          |
| `GIT_BASE_URL`     | _(empty)_     | GitHub Enterprise / GitLab self-hosted base URL.           |
| `MAX_RCA_ATTEMPTS` | `2`           | Self-heal loop cap before escalation.                      |
| `LOG_LEVEL`        | `INFO`        | Log level.                                                 |

Secrets are never committed: `.env` is gitignored and mapped to a Kubernetes Secret at deploy time.

## Docker

```bash
docker build -t opsgentic:dev .
docker run --rm -p 8080:8080 --env-file .env opsgentic:dev
```

## Status & roadmap

- **M1 (current)** — runnable skeleton: LangGraph orchestrator, three agent nodes, Validation Skills, both triggers, `interrupt_before` approval gate, FastAPI + CLI. The LLM uses a canned fallback until configured; PR creation and MCP context are stubbed.
- **M2** — wire read-only MCP (`kubernetes-mcp-server`, telemetry) for real `context_data`; add RBAC ClusterRole/ServiceAccount and `deploy/manifests/`; run as a Kubernetes Job.
- **M3** — durable checkpointing (`PostgresSaver`); real PR creation (GitHub/GitLab); approval UI.
- **M4** — automatic triggering from Grafana/Alertmanager; deeper validation skills; observability/tracing.

## Notes

- The M1 checkpointer is in-memory (single process); run `uvicorn` with one worker and expect state loss on restart. Replaced by Postgres in M3.
- Agents are designed for read-only access to the cluster. All changes flow through GitOps pull requests, gated by human approval.
