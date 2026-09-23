"""Fetch snapcraft.yaml from a GitHub packaging repository."""

from __future__ import annotations

import logging

import httpx

from snap_dashboard.github.utils import parse_owner_repo

logger = logging.getLogger(__name__)

_GH_API = "https://api.github.com"
_CANDIDATE_PATHS = [
    "snap/snapcraft.yaml",
    "snapcraft.yaml",
    ".snapcraft.yaml",
]


def fetch_snapcraft_yaml(packaging_repo: str, token: str = "") -> str | None:
    """Return the raw text of snapcraft.yaml from a GitHub repo, or None.

    ``packaging_repo`` must be in ``owner/repo`` format.  Only the GitHub URL
    scheme is supported; GitLab repos are ignored.
    """
    if not packaging_repo:
        return None
    # Accept full URLs or bare owner/repo
    owner_repo = parse_owner_repo(packaging_repo)
    if not owner_repo:
        return None
    owner, repo = owner_repo

    headers: dict[str, str] = {"Accept": "application/vnd.github.raw+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    for path in _CANDIDATE_PATHS:
        url = f"{_GH_API}/repos/{owner}/{repo}/contents/{path}"
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.text
        except httpx.RequestError as exc:
            logger.debug("fetch_snapcraft_yaml %s/%s failed: %s", owner, repo, exc)

    logger.debug("no snapcraft.yaml found in %s/%s", owner, repo)
    return None
