from __future__ import annotations

import logging

from opsgentic.config import get_settings
from opsgentic.gitops import pr as prmod

logger = logging.getLogger(__name__)


def assemble_pr_context(event: dict) -> dict:
    """Gather everything the PR-responder needs to answer a comment:

    - the original run's checkpoint state (alert, hypothesis, plan, target, workload),
      recovered via the PR url; falls back to the PR body when no run is found;
    - the PR itself (title/body/branch/base);
    - recent conversation comments;
    - the change the PR already proposes (base...branch diff).

    Network/DB access is lazy and best-effort: any missing piece degrades to empty so the
    responder can still answer from whatever context is available.
    """
    owner, repo, number = event["owner"], event["repo"], event["pr_number"]
    host = event.get("host", "github.com")

    pr = prmod.get_pr(owner, repo, number, host=host) or {}
    head_ref = ((pr.get("head") or {}).get("ref"))
    base_ref = ((pr.get("base") or {}).get("ref"))

    state = _run_state(event.get("pr_url"))
    target = state.get("gitops_target") or {}

    plan = {
        "host": host,
        "owner": owner,
        "repo": repo,
        "provider": "github",
        "target_repo": target.get("slug") or f"{owner}/{repo}",
        "path": target.get("path"),
    }
    pr_info = {"number": number, "branch": head_ref, "base": base_ref,
               "url": pr.get("html_url") or event.get("pr_url")}

    proposed_diff = ""
    if head_ref and base_ref:
        try:
            proposed_diff = prmod.proposed_diff(plan, pr_info)
        except Exception as exc:  # diff is best-effort context, not load-bearing
            logger.warning("proposed_diff failed for %s/%s#%s: %s", owner, repo, number, exc)

    comments = prmod.list_pr_comments(
        owner, repo, number, limit=get_settings().pr_responder_max_comments, host=host
    )

    return {
        "plan": plan,
        "pr_info": pr_info,
        "pr_title": pr.get("title"),
        "pr_body": pr.get("body") or "",
        "proposed_diff": proposed_diff,
        "comments": comments,
        "alert": state.get("alert_payload") or {},
        "hypothesis": state.get("hypothesis") or "",
        "remediation_plan": state.get("remediation_plan") or {},
        "gitops_target": target,
        "service_ref": state.get("service_ref") or {},
        "has_run": bool(state),
    }


def _run_state(pr_url: str | None) -> dict:
    """Checkpoint state of the run that opened this PR, or {} if none / unavailable."""
    if not pr_url or not get_settings().database_url:
        return {}
    try:
        from opsgentic import runner, runs

        row = runs.get_by_pr_url(pr_url)
        if not row:
            return {}
        snap = runner.snapshot(row["thread_id"])
        return snap.get("state") or {}
    except Exception as exc:
        logger.warning("run-state lookup failed for %s: %s", pr_url, exc)
        return {}
