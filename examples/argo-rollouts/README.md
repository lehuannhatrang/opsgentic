# Running OpsGentic with Argo Rollouts

OpsGentic can act as the agent behind
[`argoproj-labs/rollouts-plugin-metric-ai`](https://github.com/argoproj-labs/rollouts-plugin-metric-ai),
so an Argo Rollouts canary is judged by an agent that reads the cluster instead of only a
threshold — and, optionally, gets a revert PR opened for it.

No Go plugin of our own: OpsGentic implements the HTTP contract that plugin already speaks.

```
Rollouts controller
  └─ rollouts-plugin-metric-ai ──POST /a2a/analyze──▶ OpsGentic
                                                        │  1. deterministic PromQL pre-check
                                                        │  2. on breach: RCA → validation → verdict
                                                        │  3. optional revert PR
                               ◀── promote / abort + confidence + reasoning + PR link
```

---

## Before you start

| Requirement | Note |
| --- | --- |
| A cluster with OpsGentic running | `./hack/demo-up.sh` gives you one with ArgoCD + Prometheus |
| Prometheus reachable from OpsGentic | `PROMETHEUS_URL` — already set by the demo |
| `linux/amd64` nodes | The plugin release asset is amd64-only (see [Step 2](#step-2--install-the-plugin)) |
| An LLM endpoint | Without `LLM_BASE_URL` every verdict fails open and the demo proves nothing |

Everything below assumes the demo layout: OpsGentic in namespace `opsgentic`, Prometheus in
`prometheus`, the demo app in `payments`.

---

## Step 1 — Install Argo Rollouts

Pin v1.9.0: the plugin is built against that release, and the plugin API carries no
stability guarantee across versions.

```bash
kubectl create namespace argo-rollouts
kubectl apply -n argo-rollouts \
  -f https://github.com/argoproj/argo-rollouts/releases/download/v1.9.0/install.yaml

kubectl -n argo-rollouts rollout status deploy/argo-rollouts
```

The kubectl plugin is worth having for watching a rollout live:

```bash
curl -sSLo /usr/local/bin/kubectl-argo-rollouts \
  https://github.com/argoproj/argo-rollouts/releases/download/v1.9.0/kubectl-argo-rollouts-linux-amd64
chmod +x /usr/local/bin/kubectl-argo-rollouts
```

## Step 2 — Install the plugin

**The controller does not ship with it.** The binary has to be on disk in the controller pod,
or downloadable by it.

**Path A — let the controller fetch it** (simplest; needs egress to github.com):

```bash
kubectl apply -f examples/argo-rollouts/argo-rollouts-config.yaml
kubectl -n argo-rollouts rollout restart deploy/argo-rollouts
```

**Path B — initContainer** (air-gapped, or when you want the download pinned in your own
registry):

```bash
kubectl apply -f examples/argo-rollouts/argo-rollouts-config.yaml   # then edit location:
kubectl -n argo-rollouts patch deploy argo-rollouts \
  --patch-file examples/argo-rollouts/rollouts-plugin-install.yaml
```

With Path B, change `location` in the ConfigMap to `file:///plugins/metric-ai`.

> The v1.9.0 release asset is **linux/amd64 only** (62 MB, sha256
> `9da7dc6f0ed0af05b14dcdb4dda3bb264b15778f7df1c944dc0ea018efe7d51d`). On arm64 nodes —
> Apple Silicon k3d/kind — the controller will fail to execute it. Build from source or use
> an amd64 node pool.

Confirm the controller loaded it:

```bash
kubectl -n argo-rollouts logs deploy/argo-rollouts | grep -i plugin
```

## Step 3 — Point the pre-check at a metric your workload actually has

This is the step people skip, and then every analysis escalates to the LLM.

The shipped [`config/canary.yaml`](../../config/canary.yaml) assumes an HTTP service exporting
`http_requests_total` with a `code` label. The demo app is a `polinux/stress` container with
no application metrics at all, so for the demo use the memory-based variant:

```bash
kubectl -n opsgentic create configmap opsgentic-canary \
  --from-file=canary.yaml=examples/argo-rollouts/demo-canary.yaml \
  --dry-run=client -o yaml | kubectl apply -f -
```

Mount it over the baked-in file and restart:

```bash
kubectl -n opsgentic patch deploy opsgentic --type=json -p='[
  {"op":"add","path":"/spec/template/spec/volumes/-",
   "value":{"name":"canary-config","configMap":{"name":"opsgentic-canary"}}},
  {"op":"add","path":"/spec/template/spec/containers/0/volumeMounts/-",
   "value":{"name":"canary-config","mountPath":"/app/config/canary.yaml","subPath":"canary.yaml"}}
]'
kubectl -n opsgentic rollout status deploy/opsgentic
```

For a real workload, edit `config/canary.yaml` in the repo instead and rebuild the image —
the query is the single knob that decides whether a canary costs LLM tokens.

## Step 4 — Verify OpsGentic answers the plugin's contract

Before involving Rollouts at all, check the two endpoints the plugin calls:

```bash
kubectl -n opsgentic port-forward svc/opsgentic 8080:80 &

curl -s localhost:8080/ | jq .

curl -s localhost:8080/a2a/analyze -XPOST -H 'content-type: application/json' -d '{
  "userId": "argo-rollouts",
  "memoryId": "rollout:payments/payments-api",
  "prompt": "Analyze canary deployment for rollout payments-api",
  "context": {
    "namespace": "payments",
    "rolloutName": "payments-api",
    "stableSelector": "app=payments-api,rollouts-pod-template-hash=aaa",
    "canarySelector": "app=payments-api,rollouts-pod-template-hash=bbb"
  }
}' | jq .
```

You should get `{promote, confidence, analysis, rootCause, remediation}`. A healthy canary
answers `confidence: 75` with `"promoted without agent analysis"` — that is the pre-check
short-circuit working, and it means no LLM was called.

If you see `confidence: 0` and *"LLM not configured"*, set `LLM_BASE_URL`.

## Step 5 — Convert the workload to a Rollout

```bash
kubectl apply -f examples/argo-rollouts/demo-rollout.yaml
```

This replaces the `payments-api` Deployment with a Rollout plus the demo AnalysisTemplate.
Under ArgoCD, commit the change to your fork of `demo-workload` instead of applying it
directly, or ArgoCD will revert you on the next sync.

> Delete the old Deployment (`kubectl -n payments delete deploy payments-api`) or the two
> will fight over the same pods.

## Step 6 — Trigger a canary and watch

Baseline is `--vm-bytes 20M` against a 64Mi limit (~31%). Push a bad revision:

```bash
kubectl -n payments patch rollout payments-api --type=json -p='[
  {"op":"replace","path":"/spec/template/spec/containers/0/args",
   "value":["--vm","1","--vm-bytes","55M","--vm-hang","1"]}
]'

kubectl argo rollouts get rollout payments-api -n payments --watch
```

At the analysis step the canary sits near 86% of its limit, the pre-check breaches, and
OpsGentic runs a real analysis. Watch it from both sides:

```bash
# what the plugin got back
kubectl -n payments get analysisrun -o yaml | grep -A5 'measurements:'

# what OpsGentic did
kubectl -n opsgentic logs deploy/opsgentic -f | grep -i canary
```

Roll it back with the same patch and `20M`.

---

## Behaviour worth knowing before you enable it in production

**The verdict is binary.** `promote: true` becomes a successful measurement, `promote: false`
a failed one, which aborts the rollout. There is no Inconclusive phase.

**OpsGentic fails open.** Deadline exceeded, LLM unreachable, MCP down, an unhandled error —
all answer promote with confidence `0` and an explanation, never an abort. Blocking a good
deploy because the agent was slow is a worse failure than missing one bad canary.

**So run a plain Prometheus metric alongside it.** Both example templates do. That metric is
what still gates the rollout when the agent declines to conclude. It is not decoration.

**The agent only aborts on positive evidence.** Ambiguity, missing telemetry, a hypothesis it
cannot corroborate, or a problem that also affects the stable pods all resolve to
promote-with-low-confidence.

**Revert PRs are opt-in.** `A2A_REVERT_PR=true` makes a negative verdict also open a PR
reverting the workload, on top of the abort Rollouts already performs. Worth enabling under
ArgoCD: the abort restores traffic, but the bad revision is still in Git and ArgoCD will keep
reconciling toward it until a human changes the manifest. The PR is never auto-merged.

**Cross-run memory.** The plugin sends a stable `memoryId` per rollout, mapped to the
LangGraph checkpoint thread — the agent can see that this rollout already failed for a given
reason earlier. Per-run conclusions are reset each analysis; only the conversation carries.

**Cost.** The pre-check is one instant PromQL query, no LLM. A healthy canary measured every
30s for four measurements costs four Prometheus queries and nothing else. Bound the rest with
`A2A_MAX_CONCURRENCY` (default 4) and `A2A_DEADLINE_SECONDS` (default 240, and it must stay
under the plugin's 300s client timeout).

**No authentication.** `/a2a/analyze` is unauthenticated, like the other OpsGentic endpoints.
It is now a surface that can abort a production rollout — keep the Service cluster-internal
and do not expose it through an ingress.

---

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `plugin not found` in controller logs | ConfigMap not applied, or the controller was not restarted |
| `exec format error` | arm64 node running the amd64 binary |
| Every analysis calls the LLM | Pre-check query matches no metrics — check Step 3, and look for `no_data` in the OpsGentic logs |
| `confidence: 0`, *"LLM not configured"* | `LLM_BASE_URL` unset |
| AnalysisRun fails immediately | The plugin fails the run when the agent is unreachable — it has no fallback. Check the Service DNS in `agentUrl` |
| Verdict always promotes | Expected when the agent cannot establish harm. Read the `analysis` field — it says what it could not establish |
