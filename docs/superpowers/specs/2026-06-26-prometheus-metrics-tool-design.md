# Prometheus Metrics Tool for the RCA Agent — Design

## Goal

Give the read-only RCA/context enrichment agent the ability to query Prometheus
metrics during alert investigation. The agent decides query parameters
(PromQL expression, time range, step, labels) autonomously from the alert
context — the same way it already decides which Kubernetes objects to inspect.

## Approach

Add Prometheus as a third MCP server alongside the existing `kubernetes` and
`github` servers. This follows the established project principle that MCP is the
single gateway for all read-only tools (see header comment in
`mcp-config/servers.yaml`). No in-process custom tools.

The MCP server is `pab1it0/prometheus-mcp-server` (FastMCP-based, supports
Streamable HTTP transport). It exposes the tools the agent will choose among:

- `execute_query` — instant PromQL query
- `execute_range_query` — range query with start/end/step
- `list_metrics` — enumerate available metric names
- `get_metric_metadata` — type/help for a metric
- `get_targets` — scrape target health

Exact tool names/flags depend on the server version and must be confirmed with
`python -m opsgentic.mcp.diagnose` after deploy (same caveat as github-mcp).

## Scope

Prometheus tools are wired only into the **context/RCA enrichment agent**
(`_gather_async` in `src/opsgentic/mcp/context.py`). This is the only agent that
currently consumes MCP tools to investigate alerts; metrics serve root-cause
analysis directly. Validation agent is out of scope for this change.

## Components and Changes

### 1. MCP server config — `mcp-config/servers.yaml`

Add a `prometheus` block:

```yaml
  prometheus:
    transport: streamable_http
    url: ${PROMETHEUS_MCP_URL}     # e.g. http://prometheus-mcp.opsgentic.svc:8083/mcp
```

No auth headers: the MCP server reaches Prometheus directly via an in-cluster
URL with no authentication.

### 2. Deploy manifests

New files, mirroring `mcp/github-deployment.yaml` and `mcp/github-service.yaml`:

- `deploy/manifests/mcp/prometheus-deployment.yaml`
  - image `ghcr.io/pab1it0/prometheus-mcp-server:latest`
  - env:
    - `PROMETHEUS_URL` (the cluster Prometheus, supplied via configmap)
    - `PROMETHEUS_MCP_SERVER_TRANSPORT=http`
    - `PROMETHEUS_MCP_BIND_HOST=0.0.0.0`
    - `PROMETHEUS_MCP_BIND_PORT=8083`
  - containerPort 8083, readiness tcpSocket 8083
  - hardened securityContext (allowPrivilegeEscalation false, drop ALL), same
    resource requests/limits as the other MCP servers
  - no ServiceAccount: it only makes HTTP calls to Prometheus, not the K8s API
- `deploy/manifests/mcp/prometheus-service.yaml`
  - NodePort: port 8083, targetPort 8083, nodePort 31083

Register both files in `deploy/manifests/kustomization.yaml` resources list,
after the github-mcp entries.

### 3. Non-secret config — `deploy/manifests/configmap.yaml`

Add:

- `PROMETHEUS_MCP_URL: "http://prometheus-mcp.opsgentic.svc:8083/mcp"`
- `PROMETHEUS_URL: "http://kube-prometheus-stack-prometheus.prometheus.svc:9090"`

No secret entries (no-auth Prometheus).

### 4. Wire into the agent — `src/opsgentic/mcp/context.py`

- Change the include set in `_gather_async` from `include={"kubernetes"}` to
  `include={"kubernetes", "prometheus"}`.
- Do **not** add Prometheus tools to `_CONTEXT_TOOL_DENYLIST`: their output is
  compact JSON time-series. Oversized range queries are mitigated via the prompt
  (sensible step), not by blanket exclusion.

### 5. Prompt guidance — `deploy/manifests/agent-skills/sre.md`

Add brief guidance for metric use during RCA:

- Use metrics to corroborate a hypothesis (e.g. correlate a resource/error
  spike with a restart or rollout), not as a first resort.
- Center the time range on the alert timestamp; keep ranges tight.
- Prefer instant queries; for range queries pick a coarse step to avoid large
  outputs.

### 6. Diagnostics

No change to `src/opsgentic/mcp/diagnose.py` — it already iterates every server
in `servers.yaml` generically, so the new `prometheus` server is picked up
automatically.

## Data Flow

1. Alert arrives → RCA node calls `gather_context(alert)`.
2. `_gather_async` loads connections for `kubernetes` + `prometheus`, builds the
   tool list from both MCP servers.
3. `create_react_agent` runs with the `context`/`sre` prompt; the LLM chooses
   when to call Prometheus tools and with what PromQL/time range/step.
4. Tool results feed back into the agent loop; the final summary (with
   `tools_used`) is returned as context for downstream RCA.

## Error Handling

Unchanged from the existing design: `gather_context` wraps `_gather_async` in a
try/except that flattens ExceptionGroups via `explain_exception` and falls back
to a stub context. If the Prometheus MCP server is unreachable, enrichment
degrades gracefully exactly as it does today for kubernetes/github.

## Testing / Verification

- `python -m opsgentic.mcp.diagnose` lists the `prometheus` server and its tools
  (confirms transport/path and exact tool names).
- A dev/manual RCA run against a sample alert shows Prometheus tools appearing in
  the `tools_used` summary.
- `kubectl rollout restart deploy/opsgentic` after skill/config edits (loader
  reads skills once at startup).

## Out of Scope

- Authenticated/proxied Prometheus (bearer token, Thanos, Grafana Cloud).
- Prometheus tools for the validation or remediation agents.
- Alertmanager / pushgateway integration.
