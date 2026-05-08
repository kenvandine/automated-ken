"""Upstream version checkers for different source types."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

_GH_API = "https://api.github.com"


@dataclass
class UpstreamInfo:
    latest_version: str
    release_url: str = ""
    release_notes: str = ""


def get_latest_version(
    source: str,
    source_type: str,
    current_version: str,
    token: str = "",
) -> UpstreamInfo | None:
    """Return the latest upstream version, or None if undeterminable."""
    try:
        if "github.com" in source:
            return _github_latest(source, token)
        if "launchpad.net" in source:
            return _launchpad_latest(source)
        if source_type == "pypi" or "pypi.org" in source:
            return _pypi_latest(source)
        if "gitlab.com" in source:
            return _gitlab_latest(source, token)
    except Exception as exc:
        logger.warning("get_latest_version failed for %s: %s", source, exc)
    return None


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

def _github_latest(source: str, token: str = "") -> UpstreamInfo | None:
    slug = _gh_slug(source)
    if not slug:
        return None
    headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    # Try latest release first
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(f"{_GH_API}/repos/{slug}/releases/latest", headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            tag = data.get("tag_name", "")
            version = _strip_v(tag)
            notes = data.get("body", "")[:3000]
            url = data.get("html_url", "")
            return UpstreamInfo(latest_version=version, release_url=url, release_notes=notes)
    except Exception:
        pass

    # Fall back to latest tag
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(
                f"{_GH_API}/repos/{slug}/tags",
                params={"per_page": 1},
                headers=headers,
            )
        if resp.status_code == 200:
            tags = resp.json()
            if tags:
                tag = tags[0].get("name", "")
                version = _strip_v(tag)
                url = f"https://github.com/{slug}/releases/tag/{tag}"
                return UpstreamInfo(latest_version=version, release_url=url)
    except Exception:
        pass

    return None


def _gh_slug(source: str) -> str | None:
    """Extract owner/repo from a GitHub URL."""
    m = re.search(r"github\.com[/:]([^/]+/[^/\s.]+?)(?:\.git)?$", source)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# GitLab
# ---------------------------------------------------------------------------

def _gitlab_latest(source: str, token: str = "") -> UpstreamInfo | None:
    m = re.search(r"gitlab\.com/(.+?)(?:\.git)?$", source)
    if not m:
        return None
    project = m.group(1).strip("/")
    encoded = project.replace("/", "%2F")
    headers: dict[str, str] = {}
    if token:
        headers["PRIVATE-TOKEN"] = token

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(
                f"https://gitlab.com/api/v4/projects/{encoded}/releases",
                params={"per_page": 1},
                headers=headers,
            )
        if resp.status_code == 200:
            releases = resp.json()
            if releases:
                tag = releases[0].get("tag_name", "")
                version = _strip_v(tag)
                url = releases[0].get("_links", {}).get("self", "")
                notes = releases[0].get("description", "")[:3000]
                return UpstreamInfo(latest_version=version, release_url=url, release_notes=notes)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# PyPI
# ---------------------------------------------------------------------------

def _pypi_latest(source: str) -> UpstreamInfo | None:
    # Extract package name from URL or pypi:// URI
    m = re.search(r"pypi\.org/project/([^/]+)", source)
    if not m:
        m = re.match(r"pypi://([^/]+)", source)
    if not m:
        return None
    pkg = m.group(1)
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(f"https://pypi.org/pypi/{pkg}/json")
        if resp.status_code == 200:
            data = resp.json()
            version = data["info"]["version"]
            url = data["info"]["project_url"] or f"https://pypi.org/project/{pkg}/"
            return UpstreamInfo(latest_version=version, release_url=url)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Launchpad
# ---------------------------------------------------------------------------

def _launchpad_latest(source: str) -> UpstreamInfo | None:
    # Launchpad uses bazaar/git branches; extract project name and query API
    m = re.search(r"launchpad\.net/([^/\s]+)", source)
    if not m:
        return None
    project = m.group(1)
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(
                f"https://api.launchpad.net/1.0/{project}",
                headers={"Accept": "application/json"},
            )
        if resp.status_code == 200:
            data = resp.json()
            version = data.get("current_series_link", "").split("/")[-1]
            if version:
                url = f"https://launchpad.net/{project}"
                return UpstreamInfo(latest_version=version, release_url=url)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_v(tag: str) -> str:
    """Remove leading 'v' or 'V' from a version tag."""
    return tag.lstrip("vV") if tag else tag


def is_newer(latest: str, current: str) -> bool:
    """Return True if latest version string is strictly newer than current."""
    if not latest or not current:
        return bool(latest and not current)
    if latest == current:
        return False
    try:
        from packaging.version import Version  # type: ignore[import]
        return Version(latest) > Version(current)
    except Exception:
        pass
    # Simple lexicographic fallback
    return latest > current
