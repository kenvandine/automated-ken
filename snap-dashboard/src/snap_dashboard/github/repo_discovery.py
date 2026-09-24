"""Discover a snap's packaging repository by scanning the user's own GitHub repos.

The Snap Store's ``links`` metadata (issues/contact/source links) is often
empty for personal snaps that the publisher never bothered to fill in — e.g.
kenvandine's own snaps typically have no store metadata at all, even though
the packaging repo obviously exists and its ``snapcraft.yaml`` declares the
snap name. This module fills that gap: it lists the repos owned by the
authenticated GitHub user, reads each repo's ``snapcraft.yaml``, and builds a
``snap name -> packaging repo URL`` map.

Building the map requires one API call per repo (~150 for this fleet), so
results are cached in-process for a while and reused across snaps within the
same collection run instead of being rebuilt per-snap.
"""

from __future__ import annotations

import base64
import logging
import re
import time

import httpx

logger = logging.getLogger(__name__)

_GH_API = "https://api.github.com"
_SNAPCRAFT_PATHS = ("snap/snapcraft.yaml", "snapcraft.yaml", "build-aux/snap/snapcraft.yaml")
_NAME_RE = re.compile(r"^name:\s*['\"]?([A-Za-z0-9][A-Za-z0-9+._-]*)['\"]?\s*$", re.MULTILINE)

_CACHE_TTL = 3600.0  # seconds
_map_cache: dict[str, tuple[float, dict[str, str]]] = {}


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }


def _list_owned_repos(token: str) -> list[dict]:
    """Return all repos owned by the authenticated user (paginated)."""
    repos: list[dict] = []
    page = 1
    try:
        with httpx.Client(timeout=20) as client:
            while True:
                resp = client.get(
                    f"{_GH_API}/user/repos",
                    headers=_headers(token),
                    params={"affiliation": "owner", "per_page": 100, "page": page},
                )
                if resp.status_code != 200:
                    logger.warning("list_owned_repos HTTP %s", resp.status_code)
                    break
                batch = resp.json()
                if not batch:
                    break
                repos.extend(batch)
                if len(batch) < 100:
                    break
                page += 1
    except httpx.RequestError as exc:
        logger.warning("list_owned_repos request error: %s", exc)
    return repos


def _extract_snap_name(token: str, owner: str, repo: str) -> str | None:
    """Return the ``name:`` value from a repo's snapcraft.yaml, if found."""
    with httpx.Client(timeout=15) as client:
        for path in _SNAPCRAFT_PATHS:
            try:
                resp = client.get(
                    f"{_GH_API}/repos/{owner}/{repo}/contents/{path}",
                    headers=_headers(token),
                )
            except httpx.RequestError:
                continue
            if resp.status_code != 200:
                continue
            data = resp.json()
            if data.get("encoding") != "base64":
                continue
            try:
                content = base64.b64decode(data["content"]).decode(errors="replace")
            except Exception:
                continue
            match = _NAME_RE.search(content)
            if match:
                return match.group(1)
    return None


def build_packaging_repo_map(token: str, force_refresh: bool = False) -> dict[str, str]:
    """Return {snap_name: packaging_repo_url} for all of the user's own repos.

    Cached per-token for ``_CACHE_TTL`` seconds so a full fleet scan only
    happens once per collection run (or once per hour), not once per snap.
    """
    if not token:
        return {}

    cached = _map_cache.get(token)
    if cached and not force_refresh and (time.monotonic() - cached[0]) < _CACHE_TTL:
        return cached[1]

    repo_map: dict[str, str] = {}
    for repo in _list_owned_repos(token):
        if repo.get("archived") or repo.get("fork"):
            continue
        owner = repo.get("owner", {}).get("login")
        name = repo.get("name")
        html_url = repo.get("html_url")
        if not owner or not name or not html_url:
            continue
        snap_name = _extract_snap_name(token, owner, name)
        if snap_name:
            repo_map[snap_name] = html_url

    _map_cache[token] = (time.monotonic(), repo_map)
    logger.info("Discovered %d packaging repos via snapcraft.yaml scan", len(repo_map))
    return repo_map


def get_cached_packaging_repo_map(token: str) -> dict[str, str] | None:
    """Return the cached map without triggering a network scan, or None if cold.

    Useful for request-time lookups (e.g. rendering a page) where we don't
    want to block on a ~150-repo GitHub scan; the map gets warmed up by the
    background collection job instead.
    """
    cached = _map_cache.get(token)
    if cached and (time.monotonic() - cached[0]) < _CACHE_TTL:
        return cached[1]
    return None

