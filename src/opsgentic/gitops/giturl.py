from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class RepoRef:
    host: str
    slug: str    # full path minus host and .git (group/subgroup/repo for GitLab)
    owner: str   # first path segment
    repo: str    # last path segment


def parse_repo_url(url: str) -> Optional[RepoRef]:
    """Parse https/ssh git URLs into host + slug. Returns None if unparseable."""
    if not url:
        return None
    u = url.strip()
    if "://" not in u and "@" in u and ":" in u:
        # scp-like: git@host:owner/repo.git
        m = re.match(r"^[^@]+@([^:]+):(.+)$", u)
        if not m:
            return None
        host, path = m.group(1), m.group(2)
    else:
        m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://(?:[^@/]+@)?([^/]+)/(.+)$", u)
        if not m:
            return None
        host, path = m.group(1), m.group(2)
    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    segs = [s for s in path.split("/") if s]
    if len(segs) < 2:
        return None
    return RepoRef(host=host.lower(), slug="/".join(segs), owner=segs[0], repo=segs[-1])
