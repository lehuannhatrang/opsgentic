# OpsGentic — Quickstart

Three ways to run opsgentic:

- **[0) One-command demo](#0-one-command-demo)** — `./hack/demo-up.sh` brings up the cluster, ArgoCD, Prometheus, the demo apps, and opsgentic. Start here.
- **[A) Local dev](#a-local-dev)** — no cluster, fastest, for trying the graph and editing prompts.
- **[B) Kubernetes (manual)](#b-kubernetes-end-to-end)** — the full end-to-end path done by hand, for when you want to control each step.

**End state (Path B):** a failing demo workload fires an alert → Alertmanager calls opsgentic → opsgentic runs RCA → Validation → Remediation and opens a GitOps PR on **your fork** of the demo repo → you (or PR review) approve → ArgoCD applies the fix.

```
demo app (failing) --alert--> Alertmanager --webhook--> opsgentic API --queue--> worker
                                                                                    │
                                          GitHub PR on your fork  <--- remediation ─┘
                                                    │ merge
                                              ArgoCD sync --> fix applied
```

---

## Prerequisites

| For | You need |
| --- | --- |
| Local dev | Python 3.11+; a vLLM / OpenAI-compatible endpoint + key (optional — empty falls back to a canned response) |
| Kubernetes | A cluster + `kubectl`; `kustomize` (or `kubectl` ≥ 1.27 with `-k`); a container registry; **kube-prometheus-stack** (Prometheus Operator + Alertmanager); **ArgoCD**; a GitHub account |
| Both | `openssl`, `jq` (handy for the test commands) |

---

## 0. One-command demo

The fastest way to see the whole loop. One script provisions everything and wires it together:

```bash
./hack/demo-up.sh
```

It walks you through, in order:

1. **Cluster** — uses your current `kubectl` context if a cluster is reachable; otherwise offers to spin up a local **k3d / minikube / kind** cluster (handy if you don't want to install Kubernetes).
2. **GitHub auth** — prints the [fine-grained PAT](https://github.com/settings/personal-access-tokens/new) creation link (needs `Contents`, `Pull requests`, and `Administration` → Read & write), reads your token, and **forks `lehuannhatrang/demo-workload` into your account automatically**.
3. **LLM** — asks for an OpenAI-compatible endpoint; leave it blank to use the built-in canned-response fallback.
4. **Helm installs** — **ArgoCD** and **kube-prometheus-stack** (Prometheus + Alertmanager), with the Alertmanager selectors pre-configured so the demo's cross-namespace routes are picked up (no manual step B5).
5. **Demo workloads** — creates one ArgoCD Application per app in `apps/*` pointing at **your fork**, with auto-sync. The apps start failing and firing alerts.
6. **OpsGentic** — generates `bootstrap.env` from your answers and runs `./bootstrap.sh`.

The script is **idempotent** — re-run it any time. When it finishes it prints the access URLs (ArgoCD, Prometheus/Grafana, the opsgentic API + console) and a one-line `curl` to verify end-to-end. Any prompt can be pre-answered with an env var (e.g. `GITHUB_TOKEN=… LLM_BASE_URL=… ./hack/demo-up.sh`).

> The generated `bootstrap.env` contains your PAT and is gitignored — never commit it.

Tear everything down (Helm releases, opsgentic, demo apps) with:

```bash
./hack/demo-down.sh
```

It leaves your cluster running and just prints the command to delete a local cluster if you made one.

For the manual, step-by-step version of this same flow, read on (paths A and B).

---

## A. Local dev

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .
cp .env.example .env          # set LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
```

Leave `DATABASE_URL` empty: the API then runs each request **synchronously in-process** (no queue/worker), and the CLI runs the graph directly.

```bash
# one-shot via the CLI
opsgentic --file examples/grafana_alert.json --source grafana --approve

# or the HTTP service (returns the full result synchronously when DATABASE_URL is empty)
opsgentic-api                 # :8080
curl -s localhost:8080/chat -XPOST -H 'content-type: application/json' \
  -d '{"title":"t","message":"payments-api high memory","labels":{"namespace":"payments","app":"payments-api"}}' | jq .
```

> Locally, the MCP servers (cluster/repo reads) are not reachable, so RCA uses a stub context and remediation falls back to a proposal. For real RCA + manifest-editing remediation, use Path B.

---

## B. Kubernetes (end-to-end)

### B1. Fork the demo repo + ArgoCD

1. **Fork** `github.com/lehuannhatrang/demo-workload` to your account — opsgentic opens remediation PRs here.
2. In **ArgoCD**, create one Application per demo app pointing at your fork (or one `ApplicationSet` over `apps/*`):

   | App | Path | Scenario | opsgentic should fix |
   | --- | --- | --- | --- |
   | payments-api | `apps/payments-api` | memory near limit | raise `resources.limits.memory` |
   | checkout-api | `apps/checkout-api` | CPU saturation | raise `resources.limits.cpu` |
   | orders-api | `apps/orders-api` | crash-loop (early liveness probe) | raise `livenessProbe.initialDelaySeconds` |
   | inventory-api | `apps/inventory-api` | OOMKilled | raise `resources.limits.memory` |

   Sync them — the workloads start failing and fire alerts. opsgentic discovers the owning repo from these ArgoCD Applications automatically.

### B2. GitHub auth — pick one

opsgentic needs Git credentials to read your fork (via github-mcp) and open PRs.

**Default — a fine-grained PAT (easiest).** GitHub → Settings → Developer settings → **Fine-grained tokens** → Generate:
- Repository access: only your `demo-workload` fork.
- Permissions: `Contents` → Read & write, `Pull requests` → Read & write.
- Put it in `GITHUB_TOKEN`. One token covers both PRs and repo reads (`GITHUB_MCP_TOKEN` defaults to it).

**Optional — a GitHub App (production).** Short-lived installation tokens instead of a long-lived PAT:
- New GitHub App; Repository permissions `Contents`: RW + `Pull requests`: RW; webhooks off; generate a private key (`.pem`); note the **App ID**.
- Install it on your fork; note the **Installation ID** (`.../installations/<ID>`).
- Set `GITHUB_APP_ID` / `GITHUB_APP_INSTALLATION_ID` / `GITHUB_APP_PRIVATE_KEY_PATH`, and leave `GITHUB_TOKEN` empty.

### B3. Build & push the image (Optional)

```bash
docker build -t docker.io/<you>/opsgentic:dev-1.0 .
docker push docker.io/<you>/opsgentic:dev-1.0
```

Set the same value in `deploy/manifests/kustomization.yaml` (`images:` → `newName`/`newTag`) — the bootstrap script does this for you.

### B4. Bootstrap (deploy everything)

```bash
cp bootstrap.env.example bootstrap.env     # fill in: LLM, GITHUB_TOKEN (PAT) or App, image
./bootstrap.sh
```

`bootstrap.sh` (idempotent):
1. generates the `opsgentic-secrets` + `postgres-secrets` Secrets (`deploy/manifests/secrets.yaml`, gitignored) from `bootstrap.env` — the GitHub PAT (or App PEM if you chose the App) and a generated Postgres password;
2. sets the image in `kustomization.yaml`;
3. `kubectl apply -k deploy/manifests/`;
4. patches `opsgentic-config` (App IDs / model / auto-approve) and rolls out;
5. waits for Postgres, the MCP servers, the API, and the worker.

Check:

```bash
kubectl -n opsgentic get pods       # postgres, kubernetes-mcp, github-mcp, opsgentic, opsgentic-worker
```

> Always deploy with `kubectl apply -k` (kustomize), never `-f`: the base relies on a `configMapGenerator` (agent skills) and `images:`.

### B5. Wire Alertmanager → opsgentic

Each demo app ships an `AlertmanagerConfig` routing its alert to `http://opsgentic.opsgentic.svc:80/webhook/grafana` (`sendResolved: false`). For kube-prometheus-stack to pick up these **cross-namespace** configs, the Alertmanager CR must select them:

```bash
kubectl -n monitoring get alertmanager -o yaml \
  | grep -A4 -iE "alertmanagerConfigSelector|alertmanagerConfigNamespaceSelector"
```

You need `alertmanagerConfigNamespaceSelector` to include the app namespaces (or be empty = all) and `alertmanagerConfigSelector` to match label `release: kube-prometheus-stack`. (In the kube-prometheus-stack Helm values: `alertmanager.alertmanagerSpec.alertmanagerConfigNamespaceSelector: {}` and `...alertmanagerConfigSelector` matching your release label.)

### B6. Verify end-to-end

```bash
NODE=<any-node-ip>           # opsgentic Service is NodePort 31080
# fast path: synthetic chat trigger
TID=$(curl -s $NODE:31080/chat -XPOST -H 'content-type: application/json' \
  -d '{"title":"high memory","message":"payments-api high memory","labels":{"namespace":"payments","app":"payments-api"}}' | jq -r .thread_id)
curl -s $NODE:31080/runs/$TID | jq '{status, awaiting_approval, pr_url: .state.pr_url}'
```

Poll until `status` is `applied` (or `awaiting_approval` if `AUTO_APPROVE=false` → approve at `/runs/$TID/approve` or open `/ui/$TID`). A PR appears on **your fork**.

**Real path:** let an app's alert fire (e.g. inventory-api OOMKilled). Alertmanager → webhook → opsgentic enqueues → the worker runs the graph → a PR opens on your fork. Re-fires of the same alert converge on the existing PR (a comment, or one incremental commit) instead of stacking divergent commits.

---

## Editing agent skills

Agent instructions are editable markdown, not hard-coded prompts:

```
deploy/manifests/agent-skills/{sre,gitops,validation,code}.md
```

Each file has frontmatter (`name`, `description`, `agents: [...]`) + a body. The `agents` field wires a skill to agents (`rca`, `context`, `remediation`, `resolver`); the loader composes the bodies into each agent's system prompt.

To change behavior (no image rebuild):

```bash
# edit deploy/manifests/agent-skills/gitops.md
kubectl apply -k deploy/manifests/                                  # updates the opsgentic-skills ConfigMap
kubectl -n opsgentic rollout restart deploy/opsgentic deploy/opsgentic-worker
```

(Skills are read once at startup, so the restart is required.) Add a brand-new skill by dropping `agent-skills/<name>.md` with `agents: [...]` and adding it to the `configMapGenerator` in `kustomization.yaml`.

---

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `apply -k` errors on `kind: Kustomization` / `generateName` | You used `-f`. Use `kubectl apply -k deploy/manifests/`. |
| Trigger returns a result, not `202` | `DATABASE_URL` is empty → synchronous mode (no queue). Set it for async + worker. |
| Run stuck at `queued` | Worker not running: `kubectl -n opsgentic logs deploy/opsgentic-worker`. |
| Alert never reaches opsgentic | Alertmanager selectors (B5); confirm the alert is firing in Prometheus. |
| PR url is `stub://...` | GitHub App not configured (App ID / Installation ID / PEM) or no token. |
| `context_data.source = stub` / proposal-only PR | MCP servers unreachable or github-mcp auth — `kubectl -n opsgentic logs deploy/opsgentic-worker`; `python -m opsgentic.mcp.diagnose`. |

Full env reference: see [docs/USAGE.md → Configuration](docs/USAGE.md#configuration).
