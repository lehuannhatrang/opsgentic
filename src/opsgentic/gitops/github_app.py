from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

import httpx

from opsgentic.config import get_settings

logger = logging.getLogger(__name__)

# installation_id -> (token, expiry_epoch). Installation tokens last ~1h.
_cache: dict = {}


def _private_key(settings) -> Optional[str]:
    if settings.github_app_private_key_path:
        path = Path(settings.github_app_private_key_path)
        if path.exists():
            return path.read_text()
        logger.warning("GITHUB_APP_PRIVATE_KEY_PATH not found: %s", path)
    if settings.github_app_private_key:
        return settings.github_app_private_key.replace("\\n", "\n")
    return None


def github_app_token(api_base: str = "https://api.github.com") -> Optional[str]:
    """Mint (and cache) a GitHub App installation token. None if the App is not
    configured or a token cannot be obtained (caller falls back to a PAT)."""
    settings = get_settings()
    app_id = settings.github_app_id
    installation_id = settings.github_app_installation_id
    if not (app_id and installation_id):
        return None

    now = int(time.time())
    cached = _cache.get(installation_id)
    if cached and cached[1] - 60 > now:
        return cached[0]

    pem = _private_key(settings)
    if not pem:
        logger.warning("GitHub App configured but no private key available")
        return None

    try:
        import jwt
    except Exception as exc:
        logger.warning("PyJWT unavailable: %s", exc)
        return None

    assertion = jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}, pem, algorithm="RS256")
    url = f"{api_base.rstrip('/')}/app/installations/{installation_id}/access_tokens"
    try:
        resp = httpx.post(
            url,
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {assertion}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        resp.raise_for_status()
        token = resp.json()["token"]
    except Exception as exc:
        logger.warning("Failed to mint GitHub App installation token: %s", exc)
        return None

    _cache[installation_id] = (token, now + 3600)
    return token
