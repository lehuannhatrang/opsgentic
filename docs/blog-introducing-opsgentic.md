# Introducing OpsGentic: AI Agents That Turn Kubernetes Alerts into GitOps Pull Requests

Modern infrastructure teams are drowning in alerts. An on-call engineer wakes up at 3 a.m. to a `CrashLoopBackOff`, spends twenty minutes tracing logs, locates the bad config, edits a manifest, commits, and waits for ArgoCD to sync. Multiply that by the number of engineers, clusters, and alerts in your organization, and you have a significant operational burden — one that is repetitive enough to automate.

OpsGentic is an open-source **AIOps system** built to close this loop autonomously: from a fired alert to a reviewed GitOps pull request, with a human retaining final approval.

---

## The Problem: Incident Response Is Too Manual

Kubernetes keeps workloads running, but it does not tell you *why* something broke or *what* to change. That diagnostic work — correlating metrics, reading logs, reasoning about dependencies, then translating a hypothesis into a manifest edit — is left to an engineer every single time.

The tools exist to read cluster state, query observability data, and write to Git. What has been missing is a system that wires them together intelligently, produces a concrete remediation, and records every action as a reviewable change — not a live `kubectl` mutation that bypasses version control.

---

## What OpsGentic Does

OpsGentic receives an alert (from Prometheus/Alertmanager or a plain chat message), runs a stateful multi-agent pipeline to diagnose the incident, and opens a pull request on the GitOps repository that owns the affected workload. A GitOps controller (ArgoCD or Flux) applies the change after the PR is merged.

```
Prometheus/Alertmanager ──alert──▶ OpsGentic API ──enqueue──▶ Worker
                                                               │
                                             RCA → Validation → [approve] → Action
                                                               │
                   ArgoCD/Flux ◀──sync── merge ◀── Pull Request ◀┘
```

The key properties:

- **No direct cluster writes.** All reads go through read-only [MCP](https://modelcontextprotocol.io/) servers (`kubernetes-mcp-server`, `github-mcp-server`). The cluster is never mutated directly — only through a merged Git commit.
- **Every fix is a reviewable PR.** The remediation is a surgical manifest edit on a branch. Teams see exactly what changed and why before it reaches production.
- **Human in the loop.** The graph pauses before opening a PR and waits for explicit approval. Auto-approve mode exists, but even then the PR review gate remains.
- **Convergent on re-fire.** If an alert fires again while a PR is already open, OpsGentic does not stack a duplicate. It checks whether the existing proposal is still sufficient and either posts a comment or appends an incremental commit.

---

## Architecture: Three Agents, One Graph

OpsGentic uses [LangGraph](https://github.com/langchain-ai/langgraph) to implement a stateful directed graph with three core agent nodes:

### 1. RCA Agent
Receives the normalized alert payload, queries the cluster and observability data via MCP tools, and produces a root cause hypothesis. It has access to pod logs, resource descriptions, events, and metrics — all read-only.

### 2. Validation Agent
Takes the hypothesis and runs deterministic Python checks (Validation Skills) to verify it. If the hypothesis passes, it drafts a concrete remediation plan. If validation fails, the graph loops back to RCA for another attempt (up to a configurable `MAX_RCA_ATTEMPTS`). This feedback loop prevents the system from proposing fixes based on faulty reasoning.

### 3. Action Agent
Reads the GitOps repository, applies a surgical field edit to the affected manifest (preserving comments and formatting using `ruamel`), and opens a pull request. It never acts autonomously — the graph pauses at an `interrupt_before` gate before this node runs.

### Async by Default

The API is non-blocking. A webhook hits `POST /alert`, receives `202 { thread_id }`, and the actual graph run happens in a separate worker process backed by a Postgres task queue ([Procrastinate](https://procrastinate.readthedocs.io/)). Clients poll `GET /runs/{thread_id}` for status. Graph state is persisted via a `PostgresSaver` checkpointer so runs survive restarts.

For local development without a database, the graph runs synchronously in-process — no extra infrastructure needed.

---

## GitOps-Native: Multi-Repo Resolution

A single OpsGentic deployment can serve multiple GitOps repositories. When an alert fires, OpsGentic must determine which repo and which path own the affected workload. It resolves this automatically:

1. Explicit alert labels (`gitops_repo` / `gitops_path`) take priority.
2. If not present, it queries **ArgoCD** — finding the `Application` whose resources match the alerting workload.
3. Falling back to **Flux** — following the `Kustomization` and `GitRepository` chain from the workload's labels.
4. If no owner is found, the run escalates rather than guessing.

The resolved host selects a provider from `config/gitops.yaml` (GitHub, GitLab, or Gitea), using a PAT or GitHub App token for authentication.

---

## Editable Agent Skills

Agent behavior is not hard-coded. Each agent composes its system prompt from a library of **agent skill** files — plain Markdown files with YAML frontmatter that declare which agents they apply to:

```yaml
---
name: sre-context
agents: [rca, context]
---
You are an SRE agent. When analyzing an alert, always check...
```

These skill files are shipped as a Kubernetes ConfigMap (`opsgentic-skills`) mounted into the agent. To tune behavior — for example, to add cluster-specific context or restrict the agents to certain namespaces — you edit the ConfigMap and restart the worker. No image rebuild required.

---

## Bring Your Own LLM

OpsGentic requires any OpenAI-compatible endpoint. This means:

- **OpenAI** (`gpt-4o`, `gpt-4.1`, etc.)
- **Local vLLM** running a self-hosted model
- Any other provider exposing the `/v1/chat/completions` API

Configuration is purely through environment variables (`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`). There is no vendor lock-in at the LLM layer.

---

## Getting Started

**Local (no cluster needed):**

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env   # set LLM_BASE_URL / LLM_API_KEY

# run the full pipeline against a sample alert
opsgentic --file examples/grafana_alert.json --source grafana --approve
```

**Kubernetes (full demo with ArgoCD, GitHub, Alertmanager webhook):**

```bash
cp bootstrap.env.example bootstrap.env
# fill in: GITHUB_TOKEN, LLM_BASE_URL, LLM_API_KEY, GITHUB_ORG, GITOPS_REPO
./bootstrap.sh
```

The `bootstrap.sh` script installs ArgoCD, deploys the OpsGentic API and worker, registers the Alertmanager webhook, and wires everything together. See [QUICKSTART.md](../QUICKSTART.md) for the complete walkthrough.

---

## Current Status and Roadmap

OpsGentic is production-usable for teams comfortable with early-stage open-source software. The core pipeline — RCA, Validation, Remediation, human gate, PR convergence — is stable. Active work is focused on:

- Deeper validation skills (resource quota checks, dependency health, rollout history)
- Observability and tracing of agent runs
- Worker autoscaling based on queue depth

Contributions, issues, and use-case feedback are welcome. The project is licensed under **GPLv3**.

---

## Summary

OpsGentic sits at the intersection of three ideas that are each well-established but rarely combined: **AI agents** for reasoning, **MCP** for safe read-only tool access, and **GitOps** for auditable, reversible writes. The result is a system where an alert can go from firing to a reviewed pull request without anyone being paged — and where the full reasoning trail is visible in the PR description.

If your team runs Kubernetes with ArgoCD or Flux and is tired of repetitive incident response toil, OpsGentic is worth a look.

**Repository:** [github.com/lehuannhatrang/opsgentic](https://github.com/lehuannhatrang/opsgentic)
