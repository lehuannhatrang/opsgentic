# OpsGentic — Quickstart

Two ways to run opsgentic:

- **[A) Local dev](#a-local-dev)** — no cluster, fastest, for trying the graph and editing prompts.
- **[B) Kubernetes](#b-kubernetes-end-to-end)** — full end-to-end: GitHub App + ArgoCD + Alertmanager → opsgentic opens a real remediation PR.

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

### B1. Create a GitHub App

GitHub → **Settings → Developer settings → GitHub Apps → New GitHub App**:

- **Repository permissions:** `Contents` → Read & write, `Pull requests` → Read & write.
- Webhooks: uncheck "Active" (not needed).
- Create it, note the **App ID**.
- **Generate a private key** → downloads a `.pem`. Keep it safe (it is a secret).

### B2. Fork the demo repo + install the App + ArgoCD

1. **Fork** `github.com/lehuannhatrang/demo-workload` to your account.
2. **Install** your GitHub App on the fork (App page → Install App → select the fork). Note the **Installation ID** (in the install URL: `.../installations/<ID>`).
3. In **ArgoCD**, create one Application per demo app pointing at your fork (or one `ApplicationSet` over `apps/*`):

   | App | Path | Scenario | opsgentic should fix |
   | --- | --- | --- | --- |
   | payments-api | `apps/payments-api` | memory near limit | raise `resources.limits.memory` |
   | checkout-api | `apps/checkout-api` | CPU saturation | raise `resources.limits.cpu` |
   | orders-api | `apps/orders-api` | crash-loop (early liveness probe) | raise `livenessProbe.initialDelaySeconds` |
   | inventory-api | `apps/inventory-api` | OOMKilled | raise `resources.limits.memory` |

   Sync them — the workloads start failing and will fire alerts. opsgentic discovers the owning repo from these ArgoCD Applications automatically.

### B3. Build & push the image (Optional)

```bash
docker build -t docker.io/<you>/opsgentic:dev-1.0 .
docker push docker.io/<you>/opsgentic:dev-1.0
```

Set the same value in `deploy/manifests/kustomization.yaml` (`images:` → `newName`/`newTag`) — the bootstrap script does this for you.

### B4. Bootstrap (deploy everything)

```bash
cp bootstrap.env.example bootstrap.env     # fill in: LLM, App ID/Installation ID, .pem path, image, registry
./bootstrap.sh
```

`bootstrap.sh` (idempotent):
1. generates the `opsgentic-secrets` + `postgres-secrets` Secrets (`deploy/manifests/secrets.yaml`, gitignored) from `bootstrap.env` — including the GitHub App PEM and a generated Postgres password;
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

Full env reference: see [README.md](README.md#configuration).
