from __future__ import annotations

import base64
import hashlib
import urllib.parse
from typing import Optional

import httpx

from opsgentic.gitops.providers import get_provider
from opsgentic.gitops.yamledit import apply_ops


def create_pull_request(
    plan: dict,
    *,
    run_id: str = "unknown",
    hypothesis: str = "",
    validation_report: Optional[dict] = None,
    alert: Optional[dict] = None,
    edits: Optional[list] = None,
) -> str:
    """Open OR update a remediation PR/MR for the alert's issue.

    `edits` are surgical field changes ([{path, ops:[{yaml_path, value}]}]) applied to
    the real file with ruamel (comments/formatting preserved). With no usable edits it
    opens a proposal markdown instead. A stable issue key dedups: a re-fired alert
    updates the existing open PR. Without credentials, returns a stub URL.
    """
    validation_report = validation_report or {}
    alert = alert or {}
    cfg = get_provider(plan.get("host", "github.com"))
    if cfg is None:
        return _stub(plan)

    ptype = plan.get("provider") or cfg.type
    token = _bearer(ptype, cfg)
    if not token:
        return _stub(plan)

    key = _issue_key(plan, alert)
    ctx = dict(
        api_base=cfg.api_base,
        token=token,
        plan=plan,
        edits=edits,
        branch=f"opsgentic/remediation-{key}",
        proposal_path=_proposal_path(plan.get("path"), key),
        proposal=_proposal_markdown(plan, key, hypothesis, validation_report),
        title=f"[opsgentic] {plan.get('summary', 'remediation')}",
    )
    ctx["body"] = ctx["proposal"]

    if ptype == "github":
        return _github_pr(**ctx)
    if ptype == "gitea":
        return _gitea_pr(**ctx)
    if ptype == "gitlab":
        return _gitlab_mr(**ctx)
    raise NotImplementedError(f"provider '{ptype}' is not supported (github | gitea | gitlab)")


def _bearer(ptype: str, cfg) -> Optional[str]:
    if ptype == "github":
        from opsgentic.gitops.github_app import github_app_token

        return github_app_token(cfg.api_base) or cfg.token
    return cfg.token


def _stub(plan: dict) -> str:
    return f"stub://no-token/{plan.get('target_repo', 'unknown')}/pull/0"


def _issue_key(plan: dict, alert: dict) -> str:
    labels = alert.get("labels") or {}
    parts = [
        plan.get("target_repo", ""),
        plan.get("file_path", ""),
        labels.get("alertname") or alert.get("title", ""),
        labels.get("namespace", ""),
    ]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:10]


def _json(resp: httpx.Response):
    resp.raise_for_status()
    return resp.json()


def _base_or_default(plan: dict, default: Optional[str]) -> Optional[str]:
    rev = (plan.get("revision") or "").strip()
    return rev if rev and rev != "HEAD" else default


def _proposal_path(path: Optional[str], key: str) -> str:
    p = (path or "").strip("/")
    if p.endswith((".yaml", ".yml")):
        p = p.rsplit("/", 1)[0] if "/" in p else ""
    prefix = f"{p}/" if p else ""
    return f"{prefix}remediations/{key}.md"


def _proposal_markdown(plan: dict, key: str, hypothesis: str, validation_report: dict) -> str:
    checks = "\n".join(
        f"- {'PASS' if r.get('passed') else 'FAIL'} `{r.get('name')}` — {r.get('detail')}"
        for r in (validation_report.get("results") or [])
    )
    return (
        f"# Remediation proposal\n\n"
        f"- Issue key: `{key}`\n"
        f"- Risk: **{plan.get('risk', 'unknown')}**\n"
        f"- Source: `{plan.get('source', 'unknown')}`\n"
        f"- Target: `{plan.get('file_path')}` in `{plan.get('target_repo')}`\n\n"
        f"## Summary\n\n{plan.get('summary', '')}\n\n"
        f"## Root cause hypothesis\n\n{hypothesis or '(none)'}\n\n"
        f"## Validation\n\n{validation_report.get('summary', '')}\n\n{checks}\n"
    )


def _compute_changes(edits, get_file, base, proposal_path, proposal):
    """Return [(path, content)] to commit: apply agent ops to the real (base) files,
    else fall back to the proposal markdown."""
    if edits:
        out = []
        for e in edits:
            path = e.get("path")
            ops = e.get("ops") or []
            if not path or not ops:
                continue
            src, _ = get_file(path, base)
            if src is None:
                continue
            new = apply_ops(src, ops)
            if new and new != src:
                out.append((path, new))
        if out:
            return out
    return [(proposal_path, proposal)]


# --- GitHub -----------------------------------------------------------------

def _github_pr(*, api_base, token, plan, edits, branch, proposal_path, proposal, title, body) -> str:
    owner, repo = plan["owner"], plan["repo"]
    api = api_base.rstrip("/")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    with httpx.Client(timeout=30.0, headers=headers) as c:
        def get_file(path, ref):
            r = c.get(f"{api}/repos/{owner}/{repo}/contents/{path}", params={"ref": ref})
            if r.status_code == 404:
                return None, None
            r.raise_for_status()
            j = r.json()
            if isinstance(j, list):
                return None, None
            return base64.b64decode(j["content"]).decode(), j["sha"]

        def commit_file(path, content, sha):
            put = {"message": title, "content": base64.b64encode(content.encode()).decode(), "branch": branch}
            if sha:
                put["sha"] = sha
            _json(c.put(f"{api}/repos/{owner}/{repo}/contents/{path}", json=put))

        base = _base_or_default(plan, None) or _json(c.get(f"{api}/repos/{owner}/{repo}"))["default_branch"]
        if c.get(f"{api}/repos/{owner}/{repo}/git/ref/heads/{branch}").status_code == 404:
            base_sha = _json(c.get(f"{api}/repos/{owner}/{repo}/git/ref/heads/{base}"))["object"]["sha"]
            r = c.post(f"{api}/repos/{owner}/{repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": base_sha})
            if r.status_code not in (201, 422):
                r.raise_for_status()

        for path, content in _compute_changes(edits, get_file, base, proposal_path, proposal):
            current, sha = get_file(path, branch)
            if current != content:
                commit_file(path, content, sha)

        items = _json(c.get(f"{api}/repos/{owner}/{repo}/pulls", params={"head": f"{owner}:{branch}", "state": "open"}))
        if items:
            pr = items[0]
            if (pr.get("body") or "") != body:
                c.patch(f"{api}/repos/{owner}/{repo}/pulls/{pr['number']}", json={"body": body}).raise_for_status()
            return pr["html_url"]

        pr = _json(c.post(f"{api}/repos/{owner}/{repo}/pulls", json={"title": title, "head": branch, "base": base, "body": body}))
        return pr["html_url"]


# --- Gitea ------------------------------------------------------------------

def _gitea_pr(*, api_base, token, plan, edits, branch, proposal_path, proposal, title, body) -> str:
    owner, repo = plan["owner"], plan["repo"]
    api = api_base.rstrip("/")
    headers = {"Authorization": f"token {token}", "Accept": "application/json"}
    with httpx.Client(timeout=30.0, headers=headers) as c:
        def get_file(path, ref):
            r = c.get(f"{api}/repos/{owner}/{repo}/contents/{path}", params={"ref": ref})
            if r.status_code == 404:
                return None, None
            r.raise_for_status()
            j = r.json()
            if isinstance(j, list):
                return None, None
            return base64.b64decode(j["content"]).decode(), j["sha"]

        def commit_file(path, content, sha):
            payload = {"content": base64.b64encode(content.encode()).decode(), "message": title, "branch": branch}
            if sha:
                payload["sha"] = sha
                _json(c.put(f"{api}/repos/{owner}/{repo}/contents/{path}", json=payload))
            else:
                _json(c.post(f"{api}/repos/{owner}/{repo}/contents/{path}", json=payload))

        base = _base_or_default(plan, _json(c.get(f"{api}/repos/{owner}/{repo}")).get("default_branch", "main"))
        if c.get(f"{api}/repos/{owner}/{repo}/branches/{branch}").status_code == 404:
            r = c.post(f"{api}/repos/{owner}/{repo}/branches", json={"new_branch_name": branch, "old_branch_name": base})
            if r.status_code not in (201, 409):
                r.raise_for_status()

        for path, content in _compute_changes(edits, get_file, base, proposal_path, proposal):
            current, sha = get_file(path, branch)
            if current != content:
                commit_file(path, content, sha)

        opens = _json(c.get(f"{api}/repos/{owner}/{repo}/pulls", params={"state": "open"}))
        existing = next((p for p in opens if (p.get("head") or {}).get("ref") == branch), None)
        if existing:
            if (existing.get("body") or "") != body:
                c.patch(f"{api}/repos/{owner}/{repo}/pulls/{existing['number']}", json={"body": body}).raise_for_status()
            return existing.get("html_url") or existing.get("url")

        pr = _json(c.post(f"{api}/repos/{owner}/{repo}/pulls", json={"title": title, "head": branch, "base": base, "body": body}))
        return pr.get("html_url") or pr.get("url")


# --- GitLab -----------------------------------------------------------------

def _gitlab_mr(*, api_base, token, plan, edits, branch, proposal_path, proposal, title, body) -> str:
    api = api_base.rstrip("/")
    pid = urllib.parse.quote(plan["slug"], safe="")
    headers = {"PRIVATE-TOKEN": token}
    with httpx.Client(timeout=30.0, headers=headers) as c:
        def get_file(path, ref):
            enc = urllib.parse.quote(path, safe="")
            r = c.get(f"{api}/projects/{pid}/repository/files/{enc}", params={"ref": ref})
            if r.status_code == 404:
                return None, None
            r.raise_for_status()
            return base64.b64decode(r.json()["content"]).decode(), "exists"

        def commit_file(path, content, sha):
            action = "update" if sha else "create"
            commit = {"branch": branch, "commit_message": title, "actions": [{"action": action, "file_path": path, "content": content}]}
            r = c.post(f"{api}/projects/{pid}/repository/commits", json=commit)
            if r.status_code == 400:
                commit["actions"][0]["action"] = "create" if action == "update" else "update"
                r = c.post(f"{api}/projects/{pid}/repository/commits", json=commit)
            r.raise_for_status()

        base = _base_or_default(plan, _json(c.get(f"{api}/projects/{pid}")).get("default_branch", "main"))
        if c.get(f"{api}/projects/{pid}/repository/branches/{urllib.parse.quote(branch, safe='')}").status_code == 404:
            r = c.post(f"{api}/projects/{pid}/repository/branches", params={"branch": branch, "ref": base})
            if r.status_code not in (201, 400):
                r.raise_for_status()

        for path, content in _compute_changes(edits, get_file, base, proposal_path, proposal):
            current, sha = get_file(path, branch)
            if current != content:
                commit_file(path, content, sha)

        opens = _json(c.get(f"{api}/projects/{pid}/merge_requests", params={"state": "opened", "source_branch": branch}))
        if opens:
            mr = opens[0]
            if (mr.get("description") or "") != body:
                c.put(f"{api}/projects/{pid}/merge_requests/{mr['iid']}", json={"description": body}).raise_for_status()
            return mr["web_url"]

        mr = _json(c.post(
            f"{api}/projects/{pid}/merge_requests",
            json={"source_branch": branch, "target_branch": base, "title": title, "description": body},
        ))
        return mr["web_url"]


# --- Re-fire convergence (GitHub) -------------------------------------------
# When an alert re-fires and an open PR already exists, evaluate what that PR already
# proposes (base...branch) instead of recommitting a fresh — and possibly different — edit.

def _github_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def remediation_branch(plan: dict, alert: Optional[dict] = None) -> str:
    return f"opsgentic/remediation-{_issue_key(plan, alert or {})}"


def _github_ctx(plan: dict):
    """(api, owner, repo, token) for GitHub, or None if not GitHub / no token."""
    cfg = get_provider(plan.get("host", "github.com"))
    if cfg is None:
        return None
    if (plan.get("provider") or cfg.type) != "github":
        return None
    token = _bearer("github", cfg)
    if not token:
        return None
    return cfg.api_base.rstrip("/"), plan["owner"], plan["repo"], token


def find_open_remediation_pr(plan: dict, alert: Optional[dict] = None) -> Optional[dict]:
    """Return {number, url, branch, base} for this issue's open remediation PR, else None.
    GitHub only; other providers return None so the caller uses the first-fire path."""
    ctx = _github_ctx(plan)
    if ctx is None:
        return None
    api, owner, repo, token = ctx
    branch = remediation_branch(plan, alert)
    with httpx.Client(timeout=30.0, headers=_github_headers(token)) as c:
        items = _json(c.get(f"{api}/repos/{owner}/{repo}/pulls",
                            params={"head": f"{owner}:{branch}", "state": "open"}))
        if not items:
            return None
        pr = items[0]
        return {"number": pr["number"], "url": pr["html_url"],
                "branch": branch, "base": (pr.get("base") or {}).get("ref")}


def proposed_diff(plan: dict, pr_info: dict, max_chars: int = 6000) -> str:
    """The change the open PR already proposes (base...branch), as text for the agent to assess."""
    ctx = _github_ctx(plan)
    if ctx is None:
        return ""
    api, owner, repo, token = ctx
    with httpx.Client(timeout=30.0, headers=_github_headers(token)) as c:
        cmp = _json(c.get(f"{api}/repos/{owner}/{repo}/compare/{pr_info.get('base')}...{pr_info['branch']}"))
    parts = [f"--- {f.get('filename')}\n{f['patch']}" for f in cmp.get("files", []) if f.get("patch")]
    diff = "\n\n".join(parts).strip()
    return diff[:max_chars] if diff else "(the PR has no file changes yet)"


def update_remediation_pr(plan: dict, pr_info: dict, *, edits: Optional[list], reason: str = "") -> str:
    """Re-fire path: commit incremental `edits` to the branch if any actually change it; otherwise
    post one comment noting the existing proposal already covers the alert. Returns the PR URL."""
    ctx = _github_ctx(plan)
    if ctx is None:
        return pr_info.get("url", "")
    api, owner, repo, token = ctx
    branch = pr_info["branch"]
    title = f"[opsgentic] {plan.get('summary', 'remediation')}"
    with httpx.Client(timeout=30.0, headers=_github_headers(token)) as c:
        def get_file(path, ref):
            r = c.get(f"{api}/repos/{owner}/{repo}/contents/{path}", params={"ref": ref})
            if r.status_code == 404:
                return None, None
            r.raise_for_status()
            j = r.json()
            if isinstance(j, list):
                return None, None
            return base64.b64decode(j["content"]).decode(), j["sha"]

        committed = False
        for e in (edits or []):
            path, ops = e.get("path"), e.get("ops") or []
            if not path or not ops:
                continue
            src, sha = get_file(path, branch)        # apply relative to the BRANCH (incremental)
            if src is None:
                continue
            new = apply_ops(src, ops)
            if new and new != src:
                put = {"message": title, "content": base64.b64encode(new.encode()).decode(),
                       "branch": branch, "sha": sha}
                _json(c.put(f"{api}/repos/{owner}/{repo}/contents/{path}", json=put))
                committed = True

        if not committed:
            body = f"opsgentic: alert re-fired. The existing proposal already addresses it — {reason}".strip()
            _post_comment_once(c, api, owner, repo, pr_info["number"], body)
        return pr_info["url"]


def _post_comment_once(c, api, owner, repo, number, body) -> None:
    """Post an issue comment, skipping if an identical one already exists (avoid re-fire spam)."""
    existing = _json(c.get(f"{api}/repos/{owner}/{repo}/issues/{number}/comments", params={"per_page": 100}))
    if any((cm.get("body") or "").strip() == body.strip() for cm in existing):
        return
    c.post(f"{api}/repos/{owner}/{repo}/issues/{number}/comments", json={"body": body}).raise_for_status()
