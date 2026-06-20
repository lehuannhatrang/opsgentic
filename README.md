<p align="center" style="width: fit-content; margin: 0 auto; background: white;">
  <img src="docs/figures/opsgentic-logo.png" alt="logo" width="200"/>
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
3. **MCP Servers & Tooling** (`mcp-config/`, `deploy/manifests/mcp/`, `src/opsgentic/mcp`) — the single gateway for all read tools: a deployed read-only `kubernetes-mcp-server` (cluster) and `github-mcp-server` (repo). RCA context, alert→repo resolution, and the remediation agent's repo/cluster reads all go through MCP; opsgentic uses no direct Kubernetes client and no kubectl. Writes (commits/PRs) happen only in opsgentic via the GitHub App.
4. **Deployment Manifests** (`deploy/manifests/`) — Kubernetes manifests: namespace, read-only RBAC (ClusterRole/ServiceAccount), ConfigMap/Secret, Deployment + Service, Job template.

## Machine state & graph flow

```
trigger -> RCA -> resolve_target -> Validation -> [interrupt_before] -> Action -> END
                                       |                                  ^
                                       +-- (fail / unresolved repo) ------+ (self-heal loop)
```

1. **RCA** reads `alert_payload`, gathers `context_data`, writes `hypothesis`.
2. **resolve_target** maps the alerting workload to its GitOps repo/path (see Multi-repo resolution).
3. **Validation** runs the Validation Skills. On pass (and a resolved repo) it drafts a `remediation_plan` and sets `execution_status="awaiting_approval"`. On fail or unresolved repo it loops back to RCA up to `MAX_RCA_ATTEMPTS`, then escalates.
4. The graph **pauses before `action`** (`interrupt_before=["action"]`); state is persisted by the checkpointer keyed on `thread_id`. No PR exists yet.
5. A human approves; the run resumes and the **Action Agent** opens the remediation PR.

## Triggers

Both triggers normalize to the same `alert_payload`:

- **Grafana / Alertmanager webhook** — `POST /webhook/grafana`
- **User chat** (operator provides error context) — `POST /chat`

With `AUTO_APPROVE=true`, a passing validation opens the PR automatically (PR review/merge becomes the human gate); otherwise the run pauses at `awaiting_approval` for `/runs/{id}/approve` or `/ui/{id}`.

## Multi-repo resolution

opsgentic maps an alert to the GitOps repo/path that owns the affected workload, so one deployment serves many repos. Resolution chain (first match wins):

1. Explicit alert labels `gitops_repo` / `gitops_path` (override / fast path).
2. **ArgoCD** — the `Application` whose `status.resources` (or destination namespace) contains the workload → `spec.source.repoURL` + `path` + `targetRevision`.
3. **Flux** — the workload's `kustomize.toolkit.fluxcd.io/{name,namespace}` labels → `Kustomization` → `GitRepository` URL + path.
4. Unresolved → escalate (opsgentic does not guess a repo).

Discovery is deterministic — CRDs are read read-only through the **kubernetes-mcp-server** (MCP); when more than one source matches with equal confidence, the LLM picks among the candidates (hybrid).

The resolved host selects a provider from the registry (`config/gitops.yaml`): each git host maps to a type (`github` / `gitea` / `gitlab`), an API base, and the env var holding that provider's token. Use a dedicated bot/service account per provider, least-privilege over the GitOps repos. For GitHub, prefer a **GitHub App** (set `GITHUB_APP_ID` + `GITHUB_APP_INSTALLATION_ID` + private key): opsgentic mints short-lived installation tokens; a PAT (`GITHUB_TOKEN`) is the fallback. The App needs `Contents: Read & write` and `Pull requests: Read & write`.

## Project layout

```
src/opsgentic/
  config.py             # Settings (env-driven)
  agents/llm.py         # ChatOpenAI factory pointing at vLLM
  graph/
    state.py            # MachineState (TypedDict)
    builder.py          # StateGraph wiring + interrupt_before
    nodes/              # rca / resolve_target / validation / action
  skills/               # Validation Skills (registry + checks)
  mcp/                  # MCP loader + read-only context enrichment
  gitops/               # repo resolver (argocd/flux), provider registry, PR/MR
  triggers/normalize.py # Grafana + chat -> alert_payload
  runner.py             # compiled app + checkpointer + start/approve/reject
  main.py               # FastAPI service
  cli.py                # local runner
mcp-config/             # MCP server config (servers.yaml)
config/gitops.yaml      # git provider registry (host -> provider/token)
deploy/manifests/       # K8s manifests + read-only RBAC
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
| GET    | `/ui/{thread_id}`             | Minimal HTML approval page (Approve/Reject)   |

A trigger returns a `thread_id` and `awaiting_approval`; the operator then calls `approve`/`reject` or opens `/ui/{thread_id}` to review and decide.

## Configuration

| Variable           | Default       | Description                                                |
| ------------------ | ------------- | ---------------------------------------------------------- |
| `LLM_BASE_URL`     | _(empty)_     | vLLM OpenAI-compatible endpoint. Empty -> canned fallback. |
| `LLM_API_KEY`      | _(empty)_     | vLLM API key (stored in a K8s Secret when deployed).       |
| `LLM_MODEL`        | `local-model` | Model name served by vLLM.                                 |
| `LLM_TEMPERATURE`  | `0.0`         | Sampling temperature.                                      |
| `LLM_MAX_TOKENS`   | `4096`        | Max output tokens.                                         |
| `GIT_CONFIG_PATH`  | `config/gitops.yaml` | Provider registry (host -> type/api_base/token_env). |
| `GITHUB_APP_ID`    | _(empty)_     | GitHub App id (preferred GitHub auth — mints installation tokens). |
| `GITHUB_APP_INSTALLATION_ID` | _(empty)_ | GitHub App installation id.                          |
| `GITHUB_APP_PRIVATE_KEY_PATH` | _(empty)_ | Path to the App private key `.pem` (or inline `GITHUB_APP_PRIVATE_KEY`). |
| `GITHUB_TOKEN`     | _(empty)_     | Fallback GitHub PAT (used only if the App is not configured). |
| `GITEA_TOKEN`      | _(empty)_     | Gitea bot token.                                           |
| `GITLAB_TOKEN`     | _(empty)_     | GitLab bot token.                                          |
| `GIT_PROVIDER`     | `github`      | Legacy fallback type for hosts not in the registry.       |
| `GIT_TOKEN`        | _(empty)_     | Legacy fallback token for hosts not in the registry.      |
| `GIT_BASE_URL`     | _(empty)_     | Legacy fallback API base.                                  |
| `MAX_RCA_ATTEMPTS` | `2`           | Self-heal loop cap before escalation.                      |
| `AUTO_APPROVE`     | `false`       | `true` opens the PR automatically (no human approve step). |
| `MCP_ENABLED`      | `false`       | Enable read-only MCP context enrichment in the RCA agent.  |
| `MCP_CONFIG_PATH`  | `mcp-config/servers.yaml` | Path to the MCP servers config file.           |
| `DATABASE_URL`     | _(empty)_     | Postgres DSN for durable checkpoints. Empty -> in-memory.  |
| `DB_POOL_MAX_SIZE` | `10`          | Postgres connection pool size.                             |
| `LOG_LEVEL`        | `INFO`        | Log level.                                                 |

Secrets are never committed: `.env` is gitignored and mapped to a Kubernetes Secret at deploy time.

## Docker

```bash
docker build -t opsgentic:dev .
docker run --rm -p 8080:8080 --env-file .env opsgentic:dev
```

## Deploy to Kubernetes

```bash
kubectl apply -k deploy/manifests/        # namespace, read-only RBAC, kubernetes-mcp-server, ConfigMap, Deployment, Service
kubectl -n opsgentic create secret generic opsgentic-secrets \
  --from-literal=LLM_BASE_URL=http://vllm.vllm.svc:8000/v1 \
  --from-literal=LLM_API_KEY=... \
  --from-literal=GIT_TOKEN=...
```

The read-only ClusterRole is bound to the `kubernetes-mcp` ServiceAccount; the `kubernetes-mcp-server` Deployment is the **only** component with cluster API access (read-only by RBAC + `--read-only`), and opsgentic reaches the cluster solely through it. opsgentic's own ServiceAccount needs no cluster RBAC. Real values come from a Secret managed out of band (Sealed/External Secrets); `secret.example.yaml` is a template only. Adjust the `kubernetes-mcp-server` image/tag and `K8S_MCP_URL` to your environment.

## Status & roadmap

- **M1 (done)** — runnable skeleton: LangGraph orchestrator, three agent nodes, Validation Skills, both triggers, `interrupt_before` approval gate, FastAPI + CLI.
- **M2 (done)** — read-only MCP (`kubernetes-mcp-server`) wired into RCA for real `context_data`; read-only RBAC (ClusterRole/ServiceAccount) and `deploy/manifests/` (Deployment, Service, Job template).
- **M3 (done)** — durable checkpointing (`PostgresSaver`, falls back to in-memory when `DATABASE_URL` is unset); real PR creation; minimal HTML approval page at `/ui/{thread_id}`.
- **Multi-repo (done)** — alert→repo discovery via ArgoCD + Flux CRDs (deterministic, LLM tiebreak when ambiguous); multi-provider PR/MR (GitHub, Gitea, GitLab) via a host→provider registry with per-provider bot tokens.
- **Auto-remediation (done)** — the Action agent fetches the resolved manifest, generates an LLM patch (validated YAML) and commits the edited manifest in the PR; proposal-markdown fallback when the manifest can't be fetched or the patch is invalid. GitHub authenticates as a GitHub App (PAT fallback).
- **PR dedup (done)** — a stable issue key drives a deterministic branch; a re-fired alert updates the existing open PR instead of opening a duplicate.
- **MCP gateway (done)** — all cluster access (RCA context + repo resolution) routes through a deployed read-only `kubernetes-mcp-server`; no direct k8s client/kubectl. Read-only ClusterRole bound to the MCP server's ServiceAccount.
- **Agentic remediation (done)** — the Action step runs a read-only agent that reads the repo via a self-hosted `github-mcp-server` (and the cluster via `kubernetes-mcp-server`), locates the responsible manifest(s) (plain / Kustomize / Helm) and proposes multi-file edits; opsgentic commits them (agent read-only; writes via the GitHub App). Falls back to the deterministic patcher when the repo MCP server isn't reachable.
- **M4** — automatic triggering from Grafana/Alertmanager; deeper validation skills; observability/tracing.

## Notes

- Checkpointing: set `DATABASE_URL` to use the durable `PostgresSaver` (state survives restarts and lets the Deployment scale out). Without it, an in-memory checkpointer is used — single process, state lost on restart, fine for dev.
- All cluster access goes through the read-only `kubernetes-mcp-server` (MCP) — no direct Kubernetes client, no kubectl. The read-only ClusterRole is bound to the MCP server's ServiceAccount. All changes flow through GitOps pull requests, gated by human approval (or by PR review when `AUTO_APPROVE=true`). opsgentic only **opens** the PR; a GitOps controller (ArgoCD/Flux) applies the change after merge. A controller is optional — without one, set `gitops_repo`/`gitops_path` labels on alerts (the resolver's fallback), then merge the PR and apply via CI or `kubectl` yourself.
- Repeated alerts don't duplicate PRs: a stable issue key (repo + file + alertname + namespace) drives a deterministic branch, so a re-fired alert updates the existing open PR (file + description) instead of opening a new one.
