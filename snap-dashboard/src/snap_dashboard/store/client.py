"""Snap Store API client.

Uses httpx sync client with polite 1 req/s rate limiting.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

_STORE_BASE = "https://api.snapcraft.io/v2"
_HEADERS = {"Snap-Device-Series": "16"}
_RATE_LIMIT_SLEEP = 1.0  # seconds between requests

_TRACKED_CHANNELS = {"stable", "candidate", "beta", "edge"}

_last_request_time: float = 0.0


def _rate_limit() -> None:
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    if elapsed < _RATE_LIMIT_SLEEP:
        time.sleep(_RATE_LIMIT_SLEEP - elapsed)
    _last_request_time = time.monotonic()


def find_snaps_by_publisher(publisher: str) -> list[dict[str, Any]]:
    """Return a list of snap dicts published by the given publisher account."""
    _rate_limit()
    url = f"{_STORE_BASE}/snaps/find"
    # 'name' is always returned; 'publisher' and 'store-url' are valid field requests
    params = {
        "publisher": publisher,
        "fields": "publisher,store-url,links",
    }
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(url, params=params, headers=_HEADERS)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.warning("Store find_snaps_by_publisher HTTP error: %s", exc)
        return []
    except httpx.RequestError as exc:
        logger.warning("Store find_snaps_by_publisher request error: %s", exc)
        return []

    results = data.get("results", [])
    snaps: list[dict[str, Any]] = []
    for item in results:
        snap_name = item.get("name", "")
        # publisher and links are nested under item["snap"]
        snap_section = item.get("snap", {}) or {}
        pub_info = snap_section.get("publisher") or {}
        pub_username = pub_info.get("username", "") if isinstance(pub_info, dict) else ""
        snaps.append(
            {
                "name": snap_name,
                "publisher": pub_username,
            }
        )
    return snaps


def get_snap_info(snap_name: str) -> dict[str, Any]:
    """Return full snap info JSON including channel-map."""
    _rate_limit()
    url = f"{_STORE_BASE}/snaps/info/{snap_name}"
    # revision and version must be explicitly requested to appear in channel-map entries
    params = {"fields": "channel-map,links,revision,version"}
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(url, params=params, headers=_HEADERS)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as exc:
        logger.warning("Store get_snap_info %s HTTP error: %s", snap_name, exc)
        return {}
    except httpx.RequestError as exc:
        logger.warning("Store get_snap_info %s request error: %s", snap_name, exc)
        return {}


def extract_channel_map(info: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse the channel-map array into flat dicts.

    Only includes stable, candidate, beta, edge channels.
    """
    channel_map_raw = info.get("channel-map", [])
    results: list[dict[str, Any]] = []

    for entry in channel_map_raw:
        channel_info = entry.get("channel", {})
        channel_name = channel_info.get("name", "")
        # channel name may be "latest/stable" or just "stable"
        # Normalise: take the risk track suffix
        if "/" in channel_name:
            _, _, risk = channel_name.rpartition("/")
        else:
            risk = channel_name

        if risk not in _TRACKED_CHANNELS:
            continue

        architecture = channel_info.get("architecture", "amd64")
        revision = entry.get("revision")
        version = entry.get("version", "")
        released_at_str = channel_info.get("released-at") or entry.get("released-at")

        released_at: datetime | None = None
        if released_at_str:
            try:
                released_at = datetime.fromisoformat(
                    released_at_str.replace("Z", "+00:00")
                )
            except (ValueError, AttributeError):
                released_at = None

        results.append(
            {
                "channel": risk,
                "architecture": architecture,
                "revision": revision,
                "version": version,
                "released_at": released_at,
            }
        )

    return results


def _looks_like_repo(url: str) -> bool:
    """Return True if the URL looks like a GitHub or GitLab repo."""
    if not url:
        return False
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    return "github.com" in host or "gitlab.com" in host


def _base_repo_url(url: str) -> str | None:
    """Return the base repo URL, stripping trailing path segments like /issues."""
    if not url or not _looks_like_repo(url):
        return None
    parsed = urlparse(url.rstrip("/"))
    parts = [p for p in parsed.path.split("/") if p]
    # Need at least /owner/repo
    if len(parts) < 2:
        return None
    base_path = "/" + "/".join(parts[:2])
    return f"{parsed.scheme}://{parsed.netloc}{base_path}"


def extract_repo_urls(info: dict[str, Any]) -> dict[str, str | None]:
    """Extract packaging_repo and upstream_repo from snap info.

    Mapping logic:
    - ``issues`` / ``contact`` links point to where snap-specific bugs are filed
      → packaging_repo (base repo, /issues suffix stripped)
    - ``source`` links point to the upstream project source code
      → upstream_repo
    - ``website`` / ``homepage`` used as fallback if neither of the above is set

    Returns {packaging_repo, upstream_repo}.
    """
    # links may be top-level (when requested as a field) or under snap sub-object
    links: dict[str, Any] = (
        info.get("links")
        or info.get("snap", {}).get("links")
        or {}
    )

    def _first_repo(keys: list[str]) -> str | None:
        for key in keys:
            urls = links.get(key, [])
            if isinstance(urls, str):
                urls = [urls]
            for u in urls:
                repo = _base_repo_url(u)
                if repo:
                    return repo
        return None

    # Issues/contact → snap packaging repo (where snap bugs are filed)
    packaging_repo = _first_repo(["issues", "contact"])

    # Source → upstream project
    upstream_repo = _first_repo(["source", "source-code", "vcs-browser"])

    # Fallback: if neither found, use website/homepage as packaging_repo
    if not packaging_repo and not upstream_repo:
        packaging_repo = _first_repo(["website", "homepage"])
    elif not packaging_repo:
        # No issues link but have source; check website as possible packaging repo
        website = _first_repo(["website", "homepage"])
        # Only use website as packaging_repo if it differs from upstream
        if website and website.rstrip("/") != (upstream_repo or "").rstrip("/"):
            packaging_repo = website

    # If both resolved to the same repo there's no separate upstream
    if (
        packaging_repo
        and upstream_repo
        and packaging_repo.rstrip("/") == upstream_repo.rstrip("/")
    ):
        upstream_repo = None

    return {
        "packaging_repo": packaging_repo,
        "upstream_repo": upstream_repo,
    }
