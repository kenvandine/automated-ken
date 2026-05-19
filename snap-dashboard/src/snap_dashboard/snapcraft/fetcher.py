"""Fetch snapcraft.yaml from a GitHub packaging repository."""

from __future__ import annotations

import logging

import httpx

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
    repo_slug = _extract_slug(packaging_repo)
    if not repo_slug:
        return None
    owner, _, repo = repo_slug.partition("/")
    if not repo:
        return None

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


def _extract_slug(repo: str) -> str:
    """Convert a GitHub URL or owner/repo string to owner/repo."""
    repo = repo.strip()
    if repo.startswith("https://github.com/"):
        repo = repo[len("https://github.com/"):]
    elif repo.startswith("http://github.com/"):
        repo = repo[len("http://github.com/"):]
    # Strip trailing .git
    if repo.endswith(".git"):
        repo = repo[:-4]
    # Strip trailing slash
    repo = repo.rstrip("/")
    return repo
