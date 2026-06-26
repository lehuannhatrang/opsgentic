from __future__ import annotations

import hashlib
import hmac
import re

from opsgentic.gitops.pr import AGENT_FOOTER

# GitHub webhook -> PR comment trigger. Pure, unit-testable: no network, no DB.
# The normalized event is a plain dict so it serializes onto the task queue unchanged.


def verify_signature(body: bytes, header: str | None, secret: str | None) -> bool:
    """Validate the X-Hub-Signature-256 HMAC-SHA256 header. False if secret/header is
    missing or the digest does not match (constant-time compare)."""
    if not secret or not header:
        return False
    try:
        algo, sent = header.split("=", 1)
    except ValueError:
        return False
    if algo != "sha256":
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, sent)


def parse_comment_event(payload: dict) -> dict | None:
    """Normalize an `issue_comment` webhook into a comment-trigger dict, or None if it is
    not a created comment on a pull request."""
    if payload.get("action") != "created":
        return None
    issue = payload.get("issue") or {}
    if "pull_request" not in issue:          # a plain issue, not a PR
        return None
    comment = payload.get("comment") or {}
    repo = payload.get("repository") or {}
    owner = ((repo.get("owner") or {}).get("login")) or (repo.get("full_name", "/").split("/")[0])
    name = repo.get("name") or (repo.get("full_name", "/").split("/")[-1])
    user = comment.get("user") or {}
    app = comment.get("performed_via_github_app") or {}
    return {
        "owner": owner,
        "repo": name,
        "host": "github.com",
        "pr_number": issue.get("number"),
        "pr_url": (issue.get("pull_request") or {}).get("html_url") or issue.get("html_url"),
        "comment_id": str(comment.get("id")),
        "comment_body": comment.get("body") or "",
        "author_login": user.get("login"),
        "author_type": user.get("type"),
        "app_slug": app.get("slug"),
    }


def mentions_agent(body: str, handle: str) -> bool:
    """True if the comment body addresses the agent, e.g. '@opsgentic-agent ...'."""
    if not body or not handle:
        return False
    return re.search(rf"(?<![\w/])@?{re.escape(handle)}\b", body, re.IGNORECASE) is not None


def _authored_by_agent(*, body: str, login: str | None, app_slug: str | None, handle: str) -> bool:
    """Whether a comment was authored by this agent (App slug, bot login, or footer marker)."""
    if app_slug and app_slug == handle:
        return True
    if login and (login == handle or login == f"{handle}[bot]"):
        return True
    return AGENT_FOOTER.strip() in (body or "")


def is_self(event: dict, handle: str) -> bool:
    """True if the triggering comment was authored by the agent itself (avoid loops)."""
    if (event.get("author_type") or "").lower() == "bot" and event.get("app_slug") == handle:
        return True
    return _authored_by_agent(
        body=event.get("comment_body", ""),
        login=event.get("author_login"),
        app_slug=event.get("app_slug"),
        handle=handle,
    )


def should_respond(event: dict, recent_comments: list[dict], handle: str) -> bool:
    """Listen rule for flat conversation comments. Respond when, excluding the agent's own
    comments, EITHER the body @-mentions the agent OR the most recent prior comment on the
    PR was authored by the agent (the human is replying to the agent)."""
    if is_self(event, handle):
        return False
    if mentions_agent(event.get("comment_body", ""), handle):
        return True
    # Rule 2: find the comment immediately before this one and check if the agent wrote it.
    cid = event.get("comment_id")
    prior = [c for c in recent_comments if str(c.get("id")) != str(cid)]
    if not prior:
        return False
    last = prior[-1]
    return _authored_by_agent(
        body=last.get("body", ""),
        login=last.get("login"),
        app_slug=last.get("app_slug"),
        handle=handle,
    )
