from __future__ import annotations

from opsgentic.config import get_settings


def create_pull_request(plan: dict) -> str:
    """M1 stub. M3: create a branch, commit plan['diff'] into plan['file_path'], open a PR."""
    settings = get_settings()
    repo = plan.get("target_repo", "unknown")
    if not settings.git_token:
        return f"stub://no-git-token/{repo}/pull/0"
    # TODO(M3): call the GitHub/GitLab API to open a real PR.
    return f"stub://{settings.git_provider}/{repo}/pull/0"
