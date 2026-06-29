# 🤖 OpsGentic — AI Agents for DevOps Automation

<p align="center" style="width: fit-content; margin: 10 auto; background: white;">
  <img src="docs/figures/opsgentic-logo.png" alt="OpsGentic logo" width="200"/>
</p>

<p align="center">
  <b>Self-healing Kubernetes with AI agents.</b><br/>
  OpsGentic turns an alert into a reviewed GitOps pull request — automated <b>Root Cause Analysis</b>, <b>Validation</b>, and <b>Remediation</b>, with a human in the loop.
</p>

<p align="center">
  <a href="https://lehuannhatrang.github.io/opsgentic/"><img src="https://img.shields.io/badge/Website-opsgentic.io-2ea44f" alt="OpsGentic website"/></a>
  <img src="https://img.shields.io/badge/Kubernetes-GitOps-326ce5?logo=kubernetes&logoColor=white" alt="Kubernetes GitOps"/>
  <img src="https://img.shields.io/badge/LangGraph-multi--agent-1c3c3c" alt="LangGraph multi-agent"/>
  <img src="https://img.shields.io/badge/MCP-read--only-444" alt="Model Context Protocol"/>
  <img src="https://img.shields.io/badge/LLM-vLLM%20%7C%20OpenAI--compatible-412991" alt="vLLM / OpenAI-compatible LLM"/>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+"/>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-GPLv3-green" alt="License: GPLv3"/></a>
  <img src="https://img.shields.io/badge/PRs-welcome-brightgreen" alt="PRs welcome"/>
</p>

<p align="center">
  🌐 <b><a href="https://lehuannhatrang.github.io/opsgentic/">Website &amp; Documentation</a></b>
  &nbsp;·&nbsp; <a href="QUICKSTART.md">Quickstart</a>
  &nbsp;·&nbsp; <a href="docs/ARCHITECTURE.md">Architecture</a>
  &nbsp;·&nbsp; <a href="https://github.com/lehuannhatrang/opsgentic">GitHub</a>
</p>

---

> **Alert fires → agents find the root cause → propose a minimal manifest fix → open a PR on your GitOps repo → you approve → ArgoCD/Flux applies it.** 

> No kubectl, no manual patching.

**OpsGentic** is an open-source **AIOps / agentic SRE** platform for **Kubernetes incident response
and auto-remediation**. A multi-agent [LangGraph](https://github.com/langchain-ai/langgraph)
pipeline performs automated **root cause analysis** on Prometheus/Alertmanager alerts (or a chat
message), then remediates **through GitOps** — every change is a reviewable pull request opened on
your **ArgoCD/Flux** repo, never a live `kubectl` mutation. Think of it as an **AI SRE agent** that
turns alerts into self-healing infrastructure, with a human in the loop.

## ✨ Why OpsGentic

- 🧠 **Multi-agent RCA → Validation → Remediation** — a stateful LangGraph graph, not a single prompt.
- 🔧 **Auto-remediation as code** — proposes surgical manifest edits and opens a **GitOps PR** (ArgoCD/Flux applies after merge).
- 🙋 **Human-in-the-loop** — pauses for approval before any PR (or PR review when auto-approve is on).
- 🔒 **Read-only by design** — all cluster/repo reads go through [MCP](https://modelcontextprotocol.io/) servers; no direct k8s client, no kubectl. Writes happen only via Git.
- 🔁 **GitOps-native & multi-repo** — discovers the owning repo from ArgoCD/Flux; GitHub / GitLab / Gitea.
- ♻️ **Convergent** — a re-fired alert updates the existing PR (comment or one incremental commit) instead of stacking duplicates.
- ⚡ **Async** — the API enqueues and returns a `thread_id` to poll; a worker drives the run.
- ✍️ **Editable agent skills** — tune agent behavior via markdown prompts (a ConfigMap), no rebuild.
- 🧩 **Bring your own LLM** — any OpenAI-compatible endpoint (local **vLLM**, etc.), env-configured.

## 🧩 How it works

```
Prometheus/Alertmanager ─alert─▶ OpsGentic API ─enqueue─▶ Worker
                                                            │  RCA → Validation → [approve] → Action
   ArgoCD/Flux ◀─sync─ merge ◀─ Pull Request ◀──────────────┘
```

1. A Grafana/Alertmanager webhook (or `POST /chat`) is **enqueued**; the API returns `202 { thread_id }`.
2. The worker runs **RCA** (root-cause hypothesis) → **Validation** (deterministic checks) → **Action**.
3. Action reads the GitOps repo (read-only via MCP), proposes a **minimal manifest edit**, and opens a **PR**.
4. You approve (or auto-approve); ArgoCD/Flux applies the merged change.

Deep dive: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

## 🚀 Quick start

**One-command demo** — provisions the cluster (or a local k3d/minikube/kind), ArgoCD, Prometheus,
the demo apps, and opsgentic; forks the demo repo for you and only asks for a GitHub PAT:

```bash
./hack/demo-up.sh        # idempotent; ./hack/demo-down.sh to tear it down
```

**Local dev** — no cluster, just the graph:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .
cp .env.example .env          # set LLM_BASE_URL / LLM_API_KEY (empty = canned fallback)

# try the full graph locally (synchronous, no cluster needed)
opsgentic --file examples/grafana_alert.json --source grafana --approve
```

Full walkthrough — one-command demo, local dev, and the manual end-to-end Kubernetes path
(GitHub auth, ArgoCD, Alertmanager webhook, `bootstrap.sh`): **[QUICKSTART.md](QUICKSTART.md)**.

## 📚 Documentation

| Doc | What's inside |
| --- | --- |
| [Website](https://lehuannhatrang.github.io/opsgentic/) | Project homepage & docs hub |
| [QUICKSTART.md](QUICKSTART.md) | Install & run — dev + Kubernetes, GitHub auth, ArgoCD, Alertmanager webhook, troubleshooting |
| [docs/USAGE.md](docs/USAGE.md) | HTTP API, triggers, full configuration reference, editing agent skills, deploy |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Agents, async queue/worker, graph flow, remediation & convergence, multi-repo resolution |

## 🗺️ Roadmap

- ✅ Multi-agent RCA / Validation / Remediation on LangGraph, human-in-the-loop gate
- ✅ Read-only MCP gateway (kubernetes-mcp-server, github-mcp-server); no kubectl
- ✅ Agentic GitOps remediation (manifest edits) + re-fire convergence; GitHub / GitLab / Gitea
- ✅ Durable checkpoints + async task queue (Procrastinate/Postgres) + worker
- ✅ Editable agent-skill prompt library (ConfigMap)
- ⏳ Deeper validation skills; observability/tracing; worker autoscaling

## ❓ FAQ

**What is OpsGentic?**
An open-source AI agent for Kubernetes incident response. It reads Prometheus/Alertmanager alerts,
runs automated root cause analysis with a multi-agent LangGraph pipeline, and opens a GitOps pull
request with a minimal manifest fix — self-healing Kubernetes with a human in the loop.

**How does OpsGentic remediate incidents without `kubectl`?**
It never mutates the cluster directly. All cluster and repo reads go through read-only MCP servers,
and every change is written as a pull request on your GitOps repo, which ArgoCD or Flux applies
after merge.

**Which LLMs does it support?**
Any OpenAI-compatible endpoint — a self-hosted **vLLM** server, OpenAI, or other compatible APIs —
configured via environment variables.

**Does it work with ArgoCD and Flux?**
Yes. OpsGentic is GitOps-native and multi-repo: it discovers the owning repository from your
ArgoCD/Flux Applications and supports GitHub, GitLab, and Gitea.

**How do I try it quickly?**
Run `./hack/demo-up.sh` for a one-command demo (cluster + ArgoCD + Prometheus + demo apps), or use
the local-dev path with no cluster. See the **[Quickstart](QUICKSTART.md)**.

## 🤝 Contributing

Issues and PRs are welcome. Licensed under **[GPLv3](LICENSE)**.
Website & docs: **https://lehuannhatrang.github.io/opsgentic/**

---

<sub>Keywords: Kubernetes · Kubernetes incident response · SRE · DevOps · AIOps · AI SRE agent · AI agent · agentic AI · multi-agent · LLM · vLLM · LangGraph · MCP · Model Context Protocol · auto-remediation · self-healing Kubernetes · self-healing infrastructure · root cause analysis · RCA · GitOps · ArgoCD · Flux · Prometheus · Alertmanager · alert remediation · incident response automation · pull request automation</sub>

