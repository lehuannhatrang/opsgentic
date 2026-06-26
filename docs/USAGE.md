# OpsGentic — Usage

Day-to-day operation: triggers, the HTTP API, configuration, editing prompts. For a guided
setup see the [Quickstart](../QUICKSTART.md); for internals see [ARCHITECTURE.md](ARCHITECTURE.md).

## Triggers

Both triggers normalize to the same `alert_payload` and return `202 { thread_id, status: "queued", poll_url }`:

- **Grafana / Alertmanager webhook** — `POST /webhook/grafana`
- **User chat** (operator provides error context) — `POST /chat`

Poll `GET /runs/{thread_id}`. With `AUTO_APPROVE=true`, a passing validation opens the PR
automatically; otherwise the run pauses at `awaiting_approval` for `/runs/{id}/approve` (or `/ui/{id}`).

## HTTP API

| Method | Path                          | Description                                                  |
| ------ | ----------------------------- | ------------------------------------------------------------ |
| GET    | `/healthz`                    | Health check                                                 |
| POST   | `/webhook/grafana`            | Grafana/Alertmanager webhook trigger → `202 {thread_id,...}` |
| POST   | `/chat`                       | User chat trigger → `202 {thread_id,...}`                    |
| POST   | `/webhook/github`             | GitHub PR comment agent (see [PR-COMMENT-AGENT.md](PR-COMMENT-AGENT.md)) |
| GET    | `/runs/{thread_id}`           | Run status + state (`queued`/`running`/`awaiting_approval`/`applied`/`failed`) |
| POST   | `/runs/{thread_id}/approve`   | Approve the plan → `202` (resume is queued)                  |
| POST   | `/runs/{thread_id}/reject`    | Reject the plan → `202`                                      |
| GET    | `/ui/{thread_id}`             | Minimal HTML approval page (Approve/Reject)                  |

```bash
TID=$(curl -s localhost:8080/chat -XPOST -H 'content-type: application/json' \
  -d '{"title":"high memory","message":"payments-api high memory","labels":{"namespace":"payments","app":"payments-api"}}' | jq -r .thread_id)
curl -s localhost:8080/runs/$TID | jq '{status, awaiting_approval, pr_url: .state.pr_url}'
```

## Run

```bash
opsgentic-api                                              # API on :8080
opsgentic-worker                                           # queue worker (needs DATABASE_URL)
opsgentic --file examples/grafana_alert.json --source grafana --approve   # CLI, synchronous
```

Without `DATABASE_URL`, the API runs each request synchronously in-process (no worker needed).
With `DATABASE_URL`, run the worker too — it consumes the queue and drives the graph.

## Configuration

| Variable           | Default       | Description                                                |
| ------------------ | ------------- | ---------------------------------------------------------- |
| `LLM_BASE_URL`     | _(empty)_     | vLLM OpenAI-compatible endpoint. Empty -> canned fallback. |
| `LLM_API_KEY`      | _(empty)_     | vLLM API key (stored in a K8s Secret when deployed).       |
| `LLM_MODEL`        | `local-model` | Model name served by vLLM.                                 |
| `LLM_TEMPERATURE`  | `0.0`         | Sampling temperature.                                      |
| `LLM_MAX_TOKENS`   | `4096`        | Max output tokens (keep well below the model context).     |
| `SKILLS_PATH`      | `agent-skills` | Agent-skill library; auto-resolves `/app/agent-skills` in-cluster and `deploy/manifests/agent-skills` from the repo root. |
| `MCP_ENABLED`      | `false`       | Enable read-only MCP context enrichment + repo/cluster reads. |
| `MCP_CONFIG_PATH`  | `mcp-config/servers.yaml` | Path to the MCP servers config file.           |
| `MCP_RECURSION_LIMIT` | `25`       | ReAct step cap for the MCP agents.                         |
| `GIT_CONFIG_PATH`  | `config/gitops.yaml` | Provider registry (host -> type/api_base/token_env). |
| `GITHUB_TOKEN`     | _(empty)_     | Fine-grained PAT (Contents + Pull requests RW). Default GitHub auth. |
| `GITHUB_MCP_TOKEN` | _(= `GITHUB_TOKEN`)_ | Token github-mcp uses to read the repo; defaults to `GITHUB_TOKEN`. Set separately only for a least-privilege read-only token. |
| `GITHUB_APP_ID` / `GITHUB_APP_INSTALLATION_ID` | _(empty)_ | GitHub App auth (production; short-lived tokens). Takes precedence over the PAT. |
| `GITHUB_APP_PRIVATE_KEY_PATH` | _(empty)_ | Path to the App private key `.pem` (or inline `GITHUB_APP_PRIVATE_KEY`). |
| `GITEA_TOKEN` / `GITLAB_TOKEN` | _(empty)_ | Per-provider tokens (only if you target those hosts; see `config/gitops.yaml`). |
| `MAX_RCA_ATTEMPTS` | `2`           | Self-heal loop cap before escalation.                      |
| `AUTO_APPROVE`     | `false`       | `true` opens the PR automatically (review/merge is the gate). |
| `DATABASE_URL`     | _(empty)_     | Postgres DSN. Enables durable checkpoints **and** the task queue (async API + worker). Empty -> in-memory + synchronous API. |
| `DB_POOL_MAX_SIZE` | `10`          | Postgres connection pool size.                             |
| `LOG_LEVEL`        | `INFO`        | Log level.                                                 |

Secrets are never committed: `.env` is gitignored and mapped to a Kubernetes Secret at deploy time.
Non-secret values live in the `opsgentic-config` ConfigMap.

### GitHub auth

A fine-grained **PAT** (`GITHUB_TOKEN`, with `Contents` + `Pull requests` Read & write on the
target repo) is the simplest and the default. A single PAT also covers github-mcp reads
(`GITHUB_MCP_TOKEN` defaults to `GITHUB_TOKEN`). For production, a **GitHub App** is preferred —
it mints short-lived installation tokens and takes precedence over the PAT when configured.

## Editing agent skills

Agent instructions are editable markdown (`deploy/manifests/agent-skills/{sre,gitops,validation,code}.md`),
each with frontmatter (`name`, `description`, `agents: [...]`) + a body. The `agents` field wires a
skill to agents (`rca`, `context`, `remediation`, `resolver`).

Change behavior without rebuilding the image:

```bash
# edit deploy/manifests/agent-skills/gitops.md
kubectl apply -k deploy/manifests/                                  # updates the opsgentic-skills ConfigMap
kubectl -n opsgentic rollout restart deploy/opsgentic deploy/opsgentic-worker
```

(Skills are read once at startup, so the restart is required.) Add a brand-new skill by dropping
`agent-skills/<name>.md` with `agents: [...]` and adding it to the `configMapGenerator` in
`kustomization.yaml`.

## Docker

```bash
docker build -t opsgentic:dev .
docker run --rm -p 8080:8080 --env-file .env opsgentic:dev          # API
docker run --rm --env-file .env opsgentic:dev opsgentic-worker       # worker (needs DATABASE_URL)
```

## Deploy to Kubernetes

```bash
kubectl apply -k deploy/manifests/
```

Always use `-k` (kustomize), never `-f`: the base relies on a `configMapGenerator` (agent skills)
and `images:` for the app image. The app image is set once in `kustomization.yaml` (`images:` maps
the logical name `opsgentic` → registry + tag — bump `newTag` to roll a new build for both the API
and worker). Postgres backs durable checkpoints + the queue; the worker bootstraps the schema on
startup. For a full guided setup (GitHub auth, ArgoCD, Alertmanager webhook), see the
[Quickstart](../QUICKSTART.md).

## Notes

- All cluster access goes through the read-only `kubernetes-mcp-server` (MCP) — no direct Kubernetes client, no kubectl. All changes flow through GitOps pull requests, gated by human approval (or PR review when `AUTO_APPROVE=true`). OpsGentic only **opens/updates** the PR; a GitOps controller (ArgoCD/Flux) applies it after merge. Without a controller, set `gitops_repo`/`gitops_path` labels on alerts, then merge and apply yourself.
- Repeated alerts don't duplicate PRs: a stable issue key drives a deterministic branch; a re-fired alert converges on the existing open PR (a comment, or one incremental commit).
- Troubleshooting table: see [Quickstart → Troubleshooting](../QUICKSTART.md#troubleshooting).
