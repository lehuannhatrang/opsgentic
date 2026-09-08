# Argo Rollouts canary analysis via A2A — design

**Date:** 2026-08-28
**Status:** approved, implementing

## Goal

Let Argo Rollouts consult OpsGentic during canary analysis: when a canary looks bad, an agent
decides whether it is a real regression, explains why, and optionally opens a revert PR.

We reach Rollouts through the **existing** `argoproj-labs/rollouts-plugin-metric-ai` plugin
rather than writing our own Go plugin. OpsGentic implements the HTTP contract that plugin
already speaks, so integration costs zero lines of Go.

## Why not our own plugin (yet)

`rollouts-plugin-metric-ai` (Apache-2.0, active as of 2026-07) already occupies this niche in
argoproj-labs. Shipping a competing plugin fragments the ecosystem and is unlikely to be
accepted upstream. Implementing its contract instead gives us its distribution immediately.

We revisit a first-party plugin only when a concrete limitation blocks us (see
[Inherited constraints](#inherited-constraints)).

## The contract

Verified from the plugin source (`internal/plugin/a2a.go`, `internal/plugin/plugin.go`).
Despite the name, this is **not** Google's A2A protocol — it is a plain JSON POST.

```
POST {agentUrl}/a2a/analyze      Content-Type: application/json
GET  {agentUrl}/                 health check — any status code counts as healthy
```

Request:

```json
{
  "userId": "argo-rollouts",
  "memoryId": "rollout:{namespace}/{name}",
  "prompt": "Analyze canary deployment for rollout ...",
  "context": {
    "namespace": "demo",
    "rolloutName": "payments-api",
    "stableSelector": "app=payments-api,rollouts-pod-template-hash=abc",
    "canarySelector": "app=payments-api,rollouts-pod-template-hash=def",
    "extraPrompt": "optional",
    "repoUrl": "optional",
    "baseBranch": "optional"
  }
}
```

Response:

```json
{
  "analysis": "...", "rootCause": "...", "remediation": "...",
  "prLink": "https://...", "promote": true, "confidence": 87
}
```

The plugin maps the result as:

| Result | Measurement |
| --- | --- |
| `promote: true` | `AnalysisPhaseSuccessful`, `Value = confidence / 100` |
| `promote: false` | `AnalysisPhaseFailed` → rollout aborts |

## Inherited constraints

These are properties of the plugin we cannot change from our side. They are the standing
justification for a first-party plugin later.

1. **Binary verdict.** There is no `Inconclusive` path. Every response either promotes or
   aborts. An "I am not sure" answer has to be expressed as promote-with-low-confidence.
2. **Synchronous, 5-minute client timeout.** The plugin blocks on the HTTP call and does not
   use the `Run`/`Resume` async cycle that Rollouts offers. Our 202-plus-poll model does not
   apply; `/a2a/analyze` must answer within the budget.
3. **No fallback.** If the agent is unreachable the plugin fails the analysis. We cannot make
   the plugin degrade gracefully; we can only make ourselves highly available and fast.
4. **No client-side pre-check.** The plugin calls us on every measurement, so cost control
   must live on our side.

## Design

```
Rollouts controller
  └─ rollouts-plugin-metric-ai ──POST /a2a/analyze──▶ OpsGentic API
                                                        │
                          ┌─────────────────────────────┤
                          │ 1. deterministic PromQL      │  within threshold → promote, no LLM
                          │ 2. breach → analysis graph   │  rca → resolve → validation → verdict
                          │ 3. verdict.promote == false  │
                          │    and revert enabled        │  → action node opens revert PR
                          └─────────────────────────────┘
                               ◀── {promote, confidence, analysis, rootCause, remediation, prLink}
```

### Pre-check moves server-side

The cost-control decision was to wake the agent only when a cheap deterministic signal is
already unhappy. Since the plugin has no such hook, the check runs as the first thing in
`/a2a/analyze`: a single PromQL query straight to Prometheus over HTTP — no MCP, no LLM,
roughly 10ms. Within threshold, we answer `promote: true` immediately.

This is a Validation Skill in the existing sense: plain Python, deterministic, independent
of MCP.

Configured in `config/canary.yaml`:

```yaml
version: "1"
precheck:
  enabled: true
  query: |
    sum(rate(http_requests_total{namespace="{namespace}",pod=~"{canary_pods}",code=~"5.."}[2m]))
    /
    clamp_min(sum(rate(http_requests_total{namespace="{namespace}",pod=~"{canary_pods}"}[2m])), 0.001)
  threshold: 0.05
  comparison: gt
  on_missing_data: escalate    # escalate | pass
```

`on_missing_data: escalate` is the default because absent metrics are themselves suspicious;
`pass` is available for clusters where the default query does not apply.

### Analysis-only pipeline

A second blueprint, `config/pipeline.analysis.yaml`, reuses the declarative pipeline machinery.
It has no `interrupt_before` — the analysis must answer within the plugin's timeout, so there
is no human gate inside the request. The opened PR is the gate instead, exactly as with
`AUTO_APPROVE`.

```
rca → resolve_target → validation → verdict → (action | END) → END
```

`verdict` is a new node producing the promote decision. The `after_verdict` router sends the
run to `action` only when the verdict is negative *and* revert PRs are enabled.

### Verdict semantics — fail-open

When the agent cannot reach a confident negative conclusion, we answer `promote: true` with a
confidence that reflects the doubt, and put the reasoning in `analysis`.

Rationale: `promote: false` aborts a live rollout. Blocking a good deploy because the LLM was
slow or the cluster was unreadable is the fastest way to lose an operator's trust, and the
opposite risk is covered — other Prometheus metrics in the same `AnalysisTemplate` still gate
the rollout independently. We only spend the abort when we have positive evidence of harm.

Fail-open applies to: deadline exceeded, LLM unavailable, MCP unreachable, pre-check unable to
run with `on_missing_data: pass`, and any unhandled exception.

### memoryId → thread_id

The plugin sends `memoryId = "rollout:{ns}/{name}"`, stable across every AnalysisRun of a
rollout. Mapping it to the LangGraph `thread_id` gives cross-run memory for free: the
checkpointer already persists prior state, so the agent can see that this rollout failed for
reason X an hour ago.

Consequence: successive analyses of the same rollout share a checkpoint thread. The verdict
node must therefore treat prior state as history, not as current truth.

### Execution model

`/a2a/analyze` runs the analysis graph in-process via `asyncio.to_thread`, bounded by a
semaphore and an `asyncio.wait_for` deadline (default 240s, under the plugin's 300s).

**Deviation from the reviewed design**, which said enqueue-then-await. The queue buys
durability across a restart, but the verdict must return on the same HTTP connection, which
dies with the process anyway — so durability buys nothing here while adding a task type, a DB
round trip per poll, and worker-scheduling latency inside a hard deadline. The semaphore
bounds concurrency, which was the actual concern behind using the queue.

## Components

| # | Component | Location | New |
| --- | --- | --- | --- |
| 1 | `POST /a2a/analyze`, `GET /` | `main.py` | endpoints |
| 2 | Request normalize + response mapping | `triggers/a2a.py` | ✅ |
| 3 | Deterministic PromQL pre-check | `skills/canary_precheck.py` | ✅ |
| 4 | Pre-check config loader | `config/canary.yaml` | ✅ |
| 5 | Verdict node | `graph/nodes/verdict.py` | ✅ |
| 6 | `after_verdict` router | `pipeline/registry.py` | function |
| 7 | Analysis blueprint | `config/pipeline.analysis.yaml` | ✅ |
| 8 | `verdict` / `canary_ref` state | `graph/state.py` | fields |
| 9 | Analysis app + deadline | `analysis.py` | ✅ |
| 10 | Settings | `config.py` | fields |

## Settings

| Setting | Default | Purpose |
| --- | --- | --- |
| `PROMETHEUS_URL` | `None` | Direct Prometheus for the pre-check |
| `CANARY_CONFIG_PATH` | `config/canary.yaml` | Pre-check config |
| `ANALYSIS_PIPELINE_CONFIG_PATH` | `config/pipeline.analysis.yaml` | Analysis blueprint |
| `A2A_DEADLINE_SECONDS` | `240` | Hard budget, under the plugin's 300s |
| `A2A_MAX_CONCURRENCY` | `4` | Concurrent analyses |
| `A2A_REVERT_PR` | `false` | Opt-in revert PR on a negative verdict |

## Testing

Deterministic units get real tests; LLM paths are exercised through their no-LLM fallback.

- `triggers/a2a.py` — request → alert payload, verdict → response, malformed input.
- `skills/canary_precheck.py` — breach/no-breach/missing-data, placeholder substitution,
  comparison operators, HTTP failure, both `on_missing_data` modes. Prometheus stubbed.
- `graph/nodes/verdict.py` — fail-open on missing hypothesis; negative verdict when validation
  reports a real failure.
- `pipeline.analysis.yaml` — compiles, has no `interrupt_before`, routes verdict → action only
  when enabled.
- `/a2a/analyze` — happy path short-circuited by pre-check, escalation path, deadline fail-open,
  health endpoint. FastAPI `TestClient`.

## Out of scope

Argo CD Notifications adapters, the Argo CD UI extension, and a first-party Go plugin. Each is
a separate spec.
