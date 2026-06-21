# OpsGentic — Architecture

How OpsGentic turns an alert into a reviewed GitOps pull request. For setup see the
[Quickstart](../QUICKSTART.md); for day-to-day operation see [USAGE.md](USAGE.md).

## Overview

![Overview architecture](figures/overview-architecture.png)

A trigger (a Grafana/Alertmanager alert or a user chat message) hits the API, which **enqueues**
the run on a Postgres-backed task queue and returns `202 { thread_id, status, poll_url }`. A
**worker** consumes the job and runs a stateful [LangGraph](https://github.com/langchain-ai/langgraph)
graph across three core agents:

- **RCA Agent** — enriches context (via read-only MCP tools) and produces a root cause hypothesis.
- **Validation Agent** — verifies the hypothesis using deterministic Validation Skills, then drafts a remediation plan.
- **Action Agent** — opens a Git commit + pull request so a GitOps controller (ArgoCD/Flux) can apply the change.

Two tool layers feed the agents:

- **MCP Servers (read-only)** — reused open-source MCP servers (`kubernetes-mcp-server` for the cluster, `github-mcp-server` for the repo) for querying cluster/observability/repo state. No mutation happens here; writes only occur through GitOps.
- **Validation Skills** — plain-Python business logic the Validation Agent calls directly, independent of MCP.

Agent **instructions** are not hard-coded: each agent composes its system prompt from an editable
[agent-skill library](#agent-skills). The Action Agent never opens a PR autonomously — the graph
pauses at an `interrupt_before` gate for human approval (unless `AUTO_APPROVE=true`, where PR
review/merge becomes the gate).

## Async execution & the task queue

OpsGentic decouples the API from the (minutes-long) agent run:

- The API **enqueues** the run on a **Postgres-backed task queue** ([Procrastinate](https://procrastinate.readthedocs.io/)) and returns `202` immediately.
- A separate **worker** Deployment consumes the queue and drives the graph, recording the lifecycle (`queued → running → awaiting_approval → applied/failed`) in an `opsgentic_runs` table.
- Clients **poll** `GET /runs/{thread_id}` for status + state.
- The queue reuses the same Postgres used for durable checkpoints — **no extra broker**, identical locally and on Kubernetes.
- Without `DATABASE_URL` (local/dev) there is no queue: the API runs the graph **synchronously in-process** and returns the full result, and the CLI runs it directly.

## Machine state & graph flow

```
trigger -> [enqueue] -> worker:  RCA -> resolve_target -> Validation -> [interrupt_before] -> Action -> END
                                            |                                  ^
                                            +-- (fail / unresolved repo) ------+ (self-heal loop)
```

1. **RCA** reads `alert_payload`, gathers `context_data`, writes `hypothesis`.
2. **resolve_target** maps the alerting workload to its GitOps repo/path (see [Multi-repo resolution](#multi-repo-resolution)).
3. **Validation** runs the Validation Skills. On pass (and a resolved repo) it drafts a `remediation_plan` and sets `execution_status="awaiting_approval"`. On fail or unresolved repo it loops back to RCA up to `MAX_RCA_ATTEMPTS`, then escalates.
4. The graph **pauses before `action`** (`interrupt_before=["action"]`); state is persisted by the checkpointer keyed on `thread_id`. No PR exists yet.
5. A human approves (or `AUTO_APPROVE=true`); the run resumes and the **Action Agent** opens (or updates) the remediation PR.

State survives restarts via a durable **PostgresSaver** checkpointer (`DATABASE_URL`); without it, an in-memory checkpointer is used (single process, dev only).

## Remediation: surgical edits, convergence on re-fire

The Action Agent reads the resolved repo via `github-mcp-server` and proposes **surgical field
edits** (forced via structured output), which OpsGentic applies to the real manifest with
comments/formatting preserved (`ruamel`). If no edits can be produced, it falls back to a
`remediations/<key>.md` proposal.

A stable **issue key** (`repo|file|alertname|namespace`) drives a deterministic branch, so a
re-fired alert does **not** open a duplicate PR. Instead OpsGentic **converges** (GitHub):

- It reads what the open PR already proposes (the `base...branch` diff) and asks the agent whether that already resolves the alert.
- **Sufficient** → it posts one comment, no new commit.
- **Insufficient** → it commits a single incremental edit on the branch.

This avoids the divergent-commit churn from regenerating a (non-deterministic) fix on every re-fire.

## Agent skills

Agent instructions live in an editable markdown library instead of hard-coded prompts:

- `deploy/manifests/agent-skills/{sre,gitops,validation,code}.md` — each has YAML frontmatter (`name`, `description`, `agents: [...]`) plus a markdown body.
- A skill is wired to agents via its `agents` field; the loader ([`agent_skills.py`](../src/opsgentic/agent_skills.py)) composes the bodies of all skills targeting an agent into its system prompt. Code-bound mechanics (the remediation edit schema, the resolver index-only answer) stay in code.
- Wiring: `rca`/`context` → `sre`; `remediation` → `sre + gitops + validation + code`; `resolver` → `gitops`.
- Delivery: shipped as the `opsgentic-skills` **ConfigMap** (mounted at `/app/agent-skills`), so prompts are tunable without an image rebuild (a baked-in copy is the fallback). See [USAGE.md → Editing agent skills](USAGE.md#editing-agent-skills).

## Multi-repo resolution

OpsGentic maps an alert to the GitOps repo/path that owns the affected workload, so one deployment
serves many repos. Resolution chain (first match wins):

1. Explicit alert labels `gitops_repo` / `gitops_path` (override / fast path).
2. **ArgoCD** — the `Application` whose `status.resources` (or destination namespace) contains the workload → `spec.source.repoURL` + `path` + `targetRevision`.
3. **Flux** — the workload's `kustomize.toolkit.fluxcd.io/{name,namespace}` labels → `Kustomization` → `GitRepository` URL + path.
4. Unresolved → escalate (OpsGentic does not guess a repo).

The workload identity is derived from alert labels (`workload`/`deployment`/`app`, or the pod name
with its replicaset/pod suffix stripped). Discovery is deterministic — CRDs are read read-only
through the **kubernetes-mcp-server**; on an equal-confidence tie the LLM picks among candidates.

The resolved host selects a provider from the registry (`config/gitops.yaml`): each git host maps
to a type (`github` / `gitea` / `gitlab`), an API base, and the env var holding that provider's
token. Auth: a fine-grained **PAT** (`GITHUB_TOKEN`) is simplest; a **GitHub App** is preferred for
production (short-lived installation tokens). See [USAGE.md → Configuration](USAGE.md#configuration).

## Components

1. **Orchestrator & agents** (`src/opsgentic/graph`) — LangGraph `StateGraph` over a typed `MachineState`; `interrupt_before` approval gate.
2. **Validation Skills** (`src/opsgentic/skills`) — deterministic Python checks, decoupled from MCP.
3. **Agent skills** (`deploy/manifests/agent-skills`, `src/opsgentic/agent_skills.py`) — editable markdown prompt library composed per agent.
4. **MCP servers & tooling** (`mcp-config/`, `deploy/manifests/mcp/`, `src/opsgentic/mcp`) — the read-only gateway for cluster + repo reads (no direct k8s client, no kubectl).
5. **GitOps** (`src/opsgentic/gitops`) — alert→repo resolver, provider registry, PR create + re-fire convergence, remediator, YAML editing.
6. **Task queue & worker** (`src/opsgentic/tasks.py`, `runs.py`, `worker.py`) — Procrastinate over Postgres; the API enqueues, the worker executes.
7. **Deployment manifests** (`deploy/manifests/`) — namespace, read-only RBAC, MCP servers, Postgres, ConfigMap/Secret, API + worker Deployments, Service.

## Project layout

```
src/opsgentic/
  config.py             # Settings (env-driven)
  agent_skills.py       # markdown agent-skill loader (frontmatter -> per-agent prompt)
  agents/llm.py         # ChatOpenAI factory pointing at vLLM
  graph/
    state.py            # MachineState (TypedDict)
    builder.py          # StateGraph wiring + interrupt_before
    nodes/              # rca / resolve_target / validation / action
  skills/               # deterministic Validation Skills (Python)
  mcp/                  # MCP loader + read-only context enrichment
  gitops/               # resolver, provider registry, PR create + convergence, remediator, yamledit
  triggers/normalize.py # Grafana + chat -> alert_payload
  runner.py             # enqueue (async) + execute (sync) + status tracking
  tasks.py              # Procrastinate app + tasks (run_alert / resume_run)
  runs.py               # opsgentic_runs status table
  worker.py             # queue worker entrypoint (opsgentic-worker)
  main.py               # FastAPI service (async, 202 + polling)
  cli.py                # local runner (synchronous)
mcp-config/             # MCP server config (servers.yaml)
config/gitops.yaml      # git provider registry (host -> provider/token)
deploy/manifests/       # K8s manifests + agent-skills/ (-> ConfigMap)
docs/                   # ARCHITECTURE.md, USAGE.md, figures/
```
