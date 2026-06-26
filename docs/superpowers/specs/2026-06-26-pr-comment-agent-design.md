# PR Comment Agent — Design

## Goal

Let the agent participate in a remediation PR's conversation. When a human
comments on an opsgentic PR, the agent re-investigates (the PR itself, the
original alert/checkpoint context, and live Kubernetes / Prometheus / logs) and
replies to the comment:

- A question or request (e.g. "give me evidence") -> investigate live, cite
  concrete values, reply.
- A suggested change -> verify whether the suggestion is correct against
  evidence FIRST. If correct, apply it to the PR branch and reply. If not, reply
  with a reasoned rebuttal and change nothing.

This is a new interaction path that runs **parallel** to the existing
alert -> remediation graph; the graph is not modified.

## Scope

- GitHub only (the GitHub App already provides auth and webhooks).
- PR **conversation** comments only (`issue_comment`); review (code-line)
  comments are out of scope.
- The agent never merges; it only comments and (on agreement) commits to the
  existing PR branch. Merge stays a human gate.

## Decisions (confirmed during brainstorming)

- **Trigger:** GitHub webhook (`issue_comment`), real-time.
- **Listen rule (flat conversation comments have no `in_reply_to`):** the agent
  replies to an `issue_comment.created` event, after excluding comments authored
  by the agent itself, when EITHER:
  1. the comment body @-mentions the agent handle (`@<GITHUB_AGENT_HANDLE>`), OR
  2. the most recent prior comment on the PR was authored by the agent (the human
     is replying to the agent -> "a thread the agent is following").
- **Auto-apply on agreement:** when the agent judges a suggestion correct, it
  commits the edit directly to the PR branch (not a merge), consistent with the
  existing `auto_approve` behavior.
- **Public webhook exposure:** Tailscale Funnel (outbound tunnel; no public IP /
  inbound firewall changes on the private cluster).

## Architecture

```
GitHub PR comment
   -> POST /webhook/github  (HMAC verify, parse, filter, dedup)
   -> enqueue handle_pr_comment        (queue mode; inline in dev)
   -> assemble PR context              (run lookup OR PR body, comments, diff)
   -> PR-responder agent               (read-only MCP: k8s + prometheus + github + logs)
        -> {kind, reply, edits}
   -> if agree_and_edit: commit edits to PR branch
   -> post reply comment (with agent footer marker)
   -> mark comment id processed
```

## Components

### 1. Webhook ingestion — `src/opsgentic/main.py`, `src/opsgentic/triggers/github.py` (new)

`POST /webhook/github`:
- Read the raw body, verify `X-Hub-Signature-256` HMAC-SHA256 against
  `GITHUB_WEBHOOK_SECRET`. Invalid -> 401.
- Only act on `X-GitHub-Event: issue_comment`, `action == "created"`, with
  `issue.pull_request` present (it is a PR, not a plain issue). Anything else
  -> 204 no-op.
- Self-loop guard: ignore comments authored by the agent
  (`comment.performed_via_github_app.slug == <app slug>`, or `user.type == "Bot"`,
  or `user.login == GITHUB_AGENT_HANDLE`).
- Dedup: ignore if `comment.id` is already in `opsgentic_pr_events` (GitHub
  redelivers). See §6.
- If `should_respond` is false -> 204 no-op.
- Otherwise enqueue `handle_pr_comment` (queue mode) or run inline off the event
  loop (no-DB dev), mirroring `runner.enqueue`. Return 202.

`triggers/github.py` (pure, unit-testable):
- `verify_signature(body: bytes, header: str | None, secret: str) -> bool`
- `parse_comment_event(payload: dict) -> CommentEvent` — extracts owner, repo,
  pr_number, pr_url (`issue.pull_request.html_url` -> the PR `html_url`),
  comment_id, comment_body, author_login, app_slug, is_pr.
- `is_self(event, handle, app_slug) -> bool`
- `mentions_agent(body: str, handle: str) -> bool` — matches `@handle`
  (word-boundary, case-insensitive).
- `should_respond(event, recent_comments, handle, app_slug) -> bool` — applies
  the listen rule using the agent footer marker to identify the agent's prior
  comments (so it works without a bot account too).

`CommentEvent` is a small dataclass/TypedDict — the normalized comment trigger,
the analogue of `alert_payload` for this path.

### 2. Context assembly — `src/opsgentic/conversation/context.py` (new)

`assemble_pr_context(event) -> dict` returns everything the responder needs:
- `run`: `runs.get_by_pr_url(event.pr_url)` (new, §6) -> the original run row;
  then read the checkpoint snapshot for `alert_payload`, `hypothesis`,
  `remediation_plan`, `gitops_target`, `service_ref`. Reuses
  `runner.snapshot(thread_id)`.
- Fallback when no run is found: reconstruct workload/alert/issue-key from the PR
  **body** (it is rendered by `pr._proposal_markdown`, which embeds source, alert,
  workload, target, issue key).
- `comments`: the last N PR comments via `pr.list_pr_comments` (§5), for dialogue
  context. N = `PR_RESPONDER_MAX_COMMENTS` (default 20).
- `proposed_diff`: `pr.proposed_diff(plan, pr_info)` — what the PR already
  proposes (base...branch).
- `pr_info`: `{number, branch, base, url}` from `pr.find_open_remediation_pr` or a
  direct PR GET (we have owner/repo/number from the webhook).

### 3. PR-responder agent — `src/opsgentic/conversation/responder.py` (new)

A read-only ReAct agent (`create_react_agent`, `checkpointer=False`) over the
shared MCP tool set (§7): `kubernetes`, `prometheus`, and `github` (read). Unlike
alert-context enrichment, this path **allows `pods_log`** (the human may ask for
logs); the other high-volume tools stay denied.

Structured final answer (`response_format`):

```python
class PRResponse(BaseModel):
    kind: Literal["answer", "agree_and_edit", "disagree"]
    reply: str                      # markdown to post as the PR comment
    edits: list[_FieldEdit] = []    # only when kind == "agree_and_edit"
```

`_FieldEdit` is **reused** from `gitops/remediator.py` (same edit schema the
remediation flow already commits via ruamel). The conversion to
`[{path, ops:[{yaml_path, value}]}]` is the existing remediator logic, factored
into a shared `remediator.edits_to_ops(field_edits)` helper so both call sites use
one implementation.

Prompt: a new agent skill `pr-responder` composed with `sre` (SRE judgment).
Instructions:
- Ground every claim in evidence from tools or the PR; cite concrete metric
  values / event details, never a bare UI link.
- Classify the human comment:
  - request/question -> investigate live, answer (`kind=answer`).
  - suggested change -> verify correctness against evidence FIRST. If correct,
    `kind=agree_and_edit` with the minimal edit(s). If wrong/unsafe,
    `kind=disagree` with a specific, respectful rebuttal and no edits.
- Keep replies concise and engineer-to-engineer.

`respond(pr_context, comment) -> PRResponse` runs the agent and returns the
validated object. On MCP/LLM failure it returns a best-effort `answer` built from
the PR/checkpoint context (degrade, never crash the webhook path).

### 4. Reply + commit — `src/opsgentic/runner.py`, `src/opsgentic/gitops/pr.py`

`runner.handle_comment_and_track(event)`:
1. `ctx = assemble_pr_context(event)`
2. `resp = responder.respond(ctx, event)`
3. If `resp.kind == "agree_and_edit"` and `resp.edits`:
   build a minimal `plan` from `gitops_target` (or the PR), then
   `pr.update_remediation_pr(plan, pr_info, edits=ops, reason="applied human suggestion", ...)`
   to commit incrementally to the branch.
4. `pr.post_pr_comment(owner, repo, number, body=resp.reply + AGENT_FOOTER)` —
   always. A distinct reply each time (not `_post_comment_once`).
5. `pr_events.mark_processed(event.comment_id, event.pr_url)`.

New in `pr.py` (reusing `_github_ctx` / `_github_headers`):
- `get_pr(owner, repo, number) -> dict` (title, body, head branch, base).
- `list_pr_comments(owner, repo, number, limit) -> list[dict]`.
- `post_pr_comment(owner, repo, number, body) -> None` (always posts).
- `AGENT_FOOTER` constant — an HTML-comment marker
  (`<!-- opsgentic-agent -->`) appended to every agent reply so `should_respond`
  can recognize the agent's prior comments without relying on a bot login.

### 5. Task — `src/opsgentic/tasks.py`

`handle_pr_comment(event: dict)` procrastinate task on the `remediation` queue ->
`runner.handle_comment_and_track(event)`. Registered like `run_alert`.

### 6. Persistence — `src/opsgentic/runs.py`

- `get_by_pr_url(pr_url) -> dict | None`: `SELECT ... WHERE pr_url = %s ORDER BY
  updated_at DESC LIMIT 1`.
- New table `opsgentic_pr_events(comment_id text PRIMARY KEY, pr_url text,
  processed_at timestamptz DEFAULT now())`, created in `ensure_schema`
  (idempotent). `mark_processed(comment_id, pr_url)` and
  `is_processed(comment_id) -> bool`. In no-DB dev mode, dedup is a no-op
  (acceptable; GitHub does not redeliver to an inline dev run).

### 7. Shared MCP tools — `src/opsgentic/mcp/agent_tools.py` (new; refactor)

Extract the tool-loading and audit logic currently in `mcp/context.py` so three
call sites share one implementation:
- `async load_tools(include: set, *, allow: set | None, deny: set | None) ->
  (tools, tool_server)` — per-server `get_tools`, prometheus wrapping
  (`_wrap_prometheus_tool` / `_flatten_prometheus_result` / `_extract_text`
  move here), denylist filtering, and the name->server map.
- `summarize_tool_calls(messages, tool_server)` moves here too.
`mcp/context.py` and the new `responder.py` import from it. `remediator.py` keeps
its own simpler load for now (its denylist differs), but may adopt `load_tools`
later — not required for this change.

### 8. Config — `src/opsgentic/config.py`

- `github_webhook_secret: str | None` (`GITHUB_WEBHOOK_SECRET`) — secret.
- `github_agent_handle: str = "opsgentic-agent"` (`GITHUB_AGENT_HANDLE`) — the
  trigger token. NOTE: a GitHub App has no real @-mentionable handle, so this is a
  literal string the handler matches in the comment body (e.g. a human types
  `@opsgentic-agent ...`); it does not depend on GitHub's mention/notification
  system. It also doubles as the expected bot `user.login` for the self-guard.
- `pr_responder_max_comments: int = 20`.

### 9. Deploy

- `deploy/manifests/secrets.yaml` (+ `secret.example.yaml`): add
  `GITHUB_WEBHOOK_SECRET`.
- `deploy/manifests/configmap.yaml`: add `GITHUB_AGENT_HANDLE`.
- GitHub App config (out-of-band): enable the webhook with the Funnel URL +
  secret, subscribe to **Issue comment** events, ensure the App has
  `pull_requests: write` (already needed to open PRs) and `issues: write` (PR
  conversation comments use the issues comments API).
- **Tailscale Funnel** for public HTTPS to the private cluster:
  - Install the Tailscale Kubernetes Operator (Helm; OAuth client + tags) — a
    prerequisite documented in the spec, outside the kustomize base.
  - Enable Funnel for the operator's tag in the tailnet ACL (`nodeAttrs` with the
    `funnel` attribute).
  - `deploy/manifests/webhook/ingress-tailscale.yaml` (new): an `Ingress` with
    `ingressClassName: tailscale` and annotation `tailscale.com/funnel: "true"`,
    backend `opsgentic:80`, `spec.tls.hosts: [opsgentic-webhook]` ->
    public URL `https://opsgentic-webhook.<tailnet>.ts.net`. Funnel serves on
    443. Set the GitHub App webhook URL to
    `https://opsgentic-webhook.<tailnet>.ts.net/webhook/github`.
  - Registered in `kustomization.yaml` (a tunnel choice can be commented out for
    clusters that already have public ingress).

## Data flow

1. Human comments on a PR (optionally `@opsgentic-agent ...`).
2. GitHub delivers `issue_comment` to the Funnel URL -> `/webhook/github`.
3. Handler verifies HMAC, filters (PR? self? dup? should_respond?), enqueues.
4. Worker assembles context (run/checkpoint or PR body + comments + diff).
5. Responder agent investigates live and returns `{kind, reply, edits}`.
6. On `agree_and_edit`, edits are committed to the PR branch.
7. The reply is posted as a PR comment (with the agent footer marker).
8. The comment id is recorded as processed.

## Error handling

- Bad/missing signature -> 401.
- Not a PR comment / self / duplicate / should_respond false -> 204 no-op (never
  enqueue).
- Responder/MCP/LLM error -> degrade to an answer from PR/checkpoint context; if
  even that fails, post a short "couldn't process this request" comment and log.
- Commit failure on `agree_and_edit` -> post the reply explaining the intended
  change could not be committed; log. Never leave the human without a response.

## Loop safety & idempotency

- Ignore agent-authored comments (footer marker + app slug + bot type).
- Dedup by `comment.id`.
- The agent footer marker also prevents rule (2) from firing on the agent's own
  comments.

## Testing

Unit (pure functions, no network):
- `verify_signature` (valid / tampered / missing).
- `parse_comment_event` (PR vs plain issue, field extraction).
- `mentions_agent`, `is_self`, `should_respond` (mention / reply-to-agent / not
  following / self / duplicate).
- `remediator.edits_to_ops` conversion and dedup.
- `_flatten_prometheus_result` / `_extract_text` after the move (regression).

Manual/integration (like existing agent paths): a real `@agent give me evidence`
comment yields a live-investigated reply; a correct suggestion gets committed; an
incorrect suggestion gets a rebuttal with no commit.

## Out of scope

- Review (code-line) comments and their threads.
- Gitea / GitLab webhooks and comments.
- Auto-merging PRs.
- A dedicated bot user account (the App identity + footer marker suffice).
