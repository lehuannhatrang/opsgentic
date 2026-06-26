# OpsGentic — PR Comment Agent

The agent can respond to human comments on a remediation pull request. It re-investigates
(the PR diff, the original alert/checkpoint context, and live Kubernetes / Prometheus / logs)
and replies in the PR conversation:

- A question or request (e.g. `@opsgentic-agent give me evidence`) → it investigates and
  answers with concrete evidence.
- A suggested change → it verifies the suggestion against evidence first. If correct, it
  commits the change to the PR branch and replies; if not, it replies with a reasoned
  rebuttal and changes nothing.

GitHub only. PR **conversation** comments only (not review/code-line comments). The agent
never merges — merging stays a human gate.

## How it works

```
GitHub PR comment
  → POST /webhook/github   (verify HMAC, filter, dedup)
  → enqueue                (worker)
  → assemble context       (run/checkpoint or PR body, comments, base...branch diff)
  → PR-responder agent     (read-only: kubernetes + prometheus + github + logs)
  → reply  (+ commit to the PR branch if it agrees with a suggestion)
```

### When does the agent respond? (listen rule)

For each `issue_comment.created` on a PR, after excluding the agent's own comments, it
replies when **either**:

1. the comment body contains the agent handle (`@opsgentic-agent`), **or**
2. the most recent prior comment on the PR was the agent's (i.e. the human is replying to
   the agent — a thread the agent is following).

> A GitHub App has **no real @-mentionable handle**: typing `@opsgentic-agent` will not
> autocomplete or turn into a link, and that is expected. The webhook matches the **literal
> text** in the comment body, so it still triggers. The handle is configured by
> `GITHUB_AGENT_HANDLE` (default `opsgentic-agent`).

## Setup

Three parts: expose the webhook endpoint publicly, configure the GitHub App, and set the
opsgentic secret/config. The application listens on `POST /webhook/github`.

### 1. Expose `/webhook/github` over public HTTPS

GitHub must reach the endpoint from the public internet. On a private cluster (no public IP)
use an **outbound tunnel**. Below is Tailscale Funnel; any tunnel that yields a public HTTPS
URL works (Cloudflare Tunnel, ngrok, …) — just point it at the `opsgentic` Service (`:80`).

**Tailscale Funnel** (manifest: [`deploy/manifests/webhook/ingress-tailscale.yaml`](../deploy/manifests/webhook/ingress-tailscale.yaml))

1. **Install the Tailscale Kubernetes operator** (Helm). The OAuth client needs scopes
   **Devices → Core: write** and **Auth Keys: write**, tagged `tag:k8s-operator`:
   ```bash
   helm repo add tailscale https://pkgs.tailscale.com/helmcharts && helm repo update
   helm upgrade --install tailscale-operator tailscale/tailscale-operator \
     --namespace tailscale --create-namespace \
     --set-string oauth.clientId=<id> --set-string oauth.clientSecret=<secret> \
     --set-string operatorConfig.defaultTags="tag:k8s-operator"
   ```
   Get the OAuth client at **admin console → Settings → OAuth clients**.

2. **Tailnet ACL** — define tag ownership and enable Funnel **for the proxy tag**. The
   ingress proxy device (the one that actually serves Funnel) is tagged `tag:k8s` by
   default — Funnel must target `tag:k8s`, **not** `tag:k8s-operator`:
   ```jsonc
   "tagOwners": {
     "tag:k8s-operator": [],
     "tag:k8s":          ["tag:k8s-operator"],
   },
   "nodeAttrs": [
     { "target": ["tag:k8s"], "attr": ["funnel"] },
   ],
   ```
   > Wrong tag here is the most common failure: the device shows a **Funnel** badge but the
   > public DNS record is never published, so GitHub gets *"failed to connect to host"*.

3. **Admin console → DNS**: enable **MagicDNS** and **HTTPS Certificates** (two separate
   toggles; both required). Leave **Override DNS servers** off.

4. **Apply the Ingress** and wait for the operator to provision the Funnel device:
   ```bash
   kubectl apply -k deploy/manifests/
   kubectl get ingress -n opsgentic opsgentic-webhook -o wide   # ADDRESS = public hostname
   ```
   The public URL is `https://<ingress-host>.<tailnet>.ts.net` (e.g.
   `https://opsgentic-webhook.tail87cac3.ts.net`).

5. **Verify from OUTSIDE the tailnet** (a machine on the tailnet resolves the name via
   MagicDNS and will mislead you):
   ```bash
   dig @8.8.8.8 <host>.<tailnet>.ts.net A +short      # must return an IP
   curl -i https://<host>.<tailnet>.ts.net/healthz    # must return 200 {"status":"ok"}
   ```
   No public DNS after ~15 min → re-check the ACL `funnel` target (step 2), then
   `kubectl exec -n tailscale <proxy-pod> -- tailscale funnel reset` and restart the proxy
   statefulset.

### 2. Configure the GitHub App webhook

In the GitHub App settings:

- **Webhook → Active**: on.
- **Webhook URL**: `https://<host>.<tailnet>.ts.net/webhook/github`
- **Webhook secret**: a strong random string (used below as `GITHUB_WEBHOOK_SECRET`).
- **Content type**: `application/json`.
- **Permissions**: `Pull requests: Read & write` and `Issues: Read & write` (PR
  conversation comments use the issues comments API). `Contents: Read & write` is already
  needed to open PRs.
- **Subscribe to events**: **Issue comment**.

After saving, use **Advanced → Recent Deliveries → Redeliver** to send a test ping; a
healthy endpoint returns **204** for non-`issue_comment` events.

### 3. Configure opsgentic

| Key | Where | Value |
| --- | ----- | ----- |
| `GITHUB_WEBHOOK_SECRET` | `opsgentic-secrets` Secret | must match the GitHub App webhook secret |
| `GITHUB_AGENT_HANDLE`   | `opsgentic-config` ConfigMap | trigger token / bot login (default `opsgentic-agent`) |
| `PR_RESPONDER_MAX_COMMENTS` | ConfigMap (optional) | conversation history depth (default 20) |

Both the API and worker Deployments load these via `envFrom`, so a rollout restart picks
them up:
```bash
kubectl rollout restart deploy/opsgentic deploy/opsgentic-worker -n opsgentic
```
The agent posts comments and commits with the GitHub App installation token (no extra
credential). The dedup table `opsgentic_pr_events` is created automatically.

## Verify end to end

1. `curl https://<host>.<tailnet>.ts.net/healthz` → `200` from outside the tailnet.
2. On an open opsgentic PR, comment: `@opsgentic-agent give me evidence`.
3. The agent replies in the conversation within a few seconds, citing live evidence.

## Troubleshooting

| Symptom | Cause | Fix |
| ------- | ----- | --- |
| `dig` from outside tailnet → NXDOMAIN / no A record | Funnel DNS not published — ACL `funnel` attr on the wrong tag | Target `tag:k8s` (proxy tag), not `tag:k8s-operator`; enable HTTPS Certificates |
| GitHub delivery: *failed to connect to host* | Public DNS/HTTPS not live | Fix the tunnel (above); the name must resolve from a non-tailnet machine |
| Delivery returns **401** | Secret mismatch | `GITHUB_WEBHOOK_SECRET` must equal the GitHub App webhook secret |
| Delivery returns **404** | Running image lacks the endpoint | Rebuild/push the `opsgentic` image with the current code |
| Delivery returns **204**, no reply | Filtered out: not a PR comment, listen rule not satisfied, duplicate, or the agent's own comment | Comment on a **PR** (not an issue); include `@opsgentic-agent`; check it is a new comment |
| **202** but no reply appears | Worker not running the new code / task failing | `kubectl logs deploy/opsgentic-worker`; ensure the worker is on the same image |
| Can't `@mention` the app in the UI | A GitHub App is not @-mentionable | Expected — type `@opsgentic-agent` as literal text; it still triggers |

See [USAGE.md](USAGE.md) for the alert→remediation flow and [ARCHITECTURE.md](ARCHITECTURE.md)
for internals.
