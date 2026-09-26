"""Upstream version checkers for different source types.

Historically this compared tags with a leading-``v`` regex strip plus
``packaging.version.Version()``. That breaks constantly in the wild — tag
schemes like ``release-2024.03``, ``v1.2.3_stable``, date-based tags mixed
with semver tags in the same repo, pre-release tags that sort "higher" than
a real release, monorepo tags prefixed with a component name, etc. all
either raise inside ``packaging.version`` (silently swallowed, falling back
to a lexicographic compare that is *also* wrong for those same schemes) or
just get the "is this newer" call wrong outright.

So a local LLM is now the **primary** decision-maker: each source-type
fetcher gathers a handful of the most recent raw tags/releases and hands
them to :func:`choose_latest_version`, which asks the model which one (if
any) is a genuine, newer, non-prerelease bump over the current version. The
old regex/``packaging.version`` logic (:func:`is_newer`) still exists, but
now only as the fallback used when no local model is available/reachable —
it is no longer how this decision gets made day to day.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

_GH_API = "https://api.github.com"

# How many recent tags/releases to show the model — enough context to
# reason about a repo's tagging scheme without blowing the prompt budget.
_CANDIDATE_LIMIT = 10


@dataclass
class UpstreamInfo:
    latest_version: str
    release_url: str = ""
    release_notes: str = ""


@dataclass
class _Candidate:
    tag: str
    version: str
    url: str = ""
    notes: str = ""
    prerelease: bool = False


def get_latest_version(
    source: str,
    source_type: str,
    current_version: str,
    token: str = "",
    user_config=None,
) -> UpstreamInfo | None:
    """Return the latest upstream version that's genuinely newer, or None.

    Fetches a handful of recent tags/releases from the source, then asks a
    local model (when available — see ``user_config``) to decide which one,
    if any, represents a real newer version to bump to. Falls back to a
    deterministic regex/``packaging.version`` heuristic when no model is
    reachable.
    """
    try:
        candidates: list[_Candidate] = []
        if "github.com" in source:
            candidates = _github_candidates(source, token)
        elif "launchpad.net" in source:
            candidates = _launchpad_candidates(source)
        elif source_type == "pypi" or "pypi.org" in source:
            candidates = _pypi_candidates(source)
        elif "gitlab.com" in source:
            candidates = _gitlab_candidates(source, token)
    except Exception as exc:
        logger.warning("get_latest_version failed for %s: %s", source, exc)
        return None

    if not candidates:
        return None

    chosen = choose_latest_version(candidates, current_version, user_config)
    if not chosen:
        return None
    return UpstreamInfo(
        latest_version=chosen.version,
        release_url=chosen.url,
        release_notes=chosen.notes,
    )


# ---------------------------------------------------------------------------
# Model-driven decision (primary path)
# ---------------------------------------------------------------------------

def choose_latest_version(
    candidates: list[_Candidate], current_version: str, user_config=None,
) -> _Candidate | None:
    """Ask a local model which candidate (if any) is a genuine newer release.

    This is deliberately the primary way "is there a new upstream version"
    gets decided — see module docstring. Falls back to
    :func:`_heuristic_choose` (the old regex/``packaging.version`` compare)
    when no local model is available or it fails to give a usable answer.
    """
    if not candidates:
        return None

    from snap_dashboard.lemonade.client import get_lemonade_client

    client = get_lemonade_client(user_config, task="text") if user_config else None
    if client is None or not client.is_available():
        return _heuristic_choose(candidates, current_version)

    listing = "\n".join(
        f"- tag: {c.tag!r}, version: {c.version!r}"
        f"{' (marked prerelease)' if c.prerelease else ''}"
        for c in candidates
    )
    prompt = (
        f"An upstream project's packaging currently tracks version "
        f"{current_version!r}. Here are its most recent tags/releases, newest "
        f"first:\n\n{listing}\n\n"
        "Decide whether any of these represents a genuinely newer, stable "
        "release the packaging should be bumped to. Use your judgment about "
        "the project's own version-numbering scheme (semver, date-based, "
        "component-prefixed, etc.) rather than assuming a specific format. "
        "Skip pre-release/beta/rc/alpha/nightly tags unless the current "
        "version is itself a pre-release. If nothing is newer, say so.\n\n"
        'Respond with ONLY a JSON object: {"is_newer": true|false, "tag": '
        '"<the exact tag string from the list above, or empty if not newer>"}'
    )
    reply = client.chat(prompt, temperature=0.1)
    if not reply:
        return _heuristic_choose(candidates, current_version)
    try:
        start = reply.find("{")
        end = reply.rfind("}") + 1
        data = json.loads(reply[start:end])
    except Exception:
        return _heuristic_choose(candidates, current_version)

    if not data.get("is_newer"):
        return None
    chosen_tag = data.get("tag", "")
    for c in candidates:
        if c.tag == chosen_tag:
            return c
    # Model said "newer" but didn't name a recognizable tag — don't guess.
    logger.warning(
        "choose_latest_version: model said is_newer=true but returned an "
        "unrecognized tag %r — falling back to heuristic", chosen_tag,
    )
    return _heuristic_choose(candidates, current_version)


def _heuristic_choose(candidates: list[_Candidate], current_version: str) -> _Candidate | None:
    """Deterministic fallback: first non-prerelease candidate newer than current."""
    for c in candidates:
        if c.prerelease:
            continue
        if is_newer(c.version, current_version):
            return c
    return None


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

def _github_candidates(source: str, token: str = "") -> list[_Candidate]:
    slug = _gh_slug(source)
    if not slug:
        return []
    headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    candidates: list[_Candidate] = []
    seen_tags: set[str] = set()

    # Formal releases first — they carry notes and an explicit prerelease flag.
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(
                f"{_GH_API}/repos/{slug}/releases",
                params={"per_page": _CANDIDATE_LIMIT},
                headers=headers,
            )
        if resp.status_code == 200:
            for rel in resp.json():
                tag = rel.get("tag_name", "")
                if not tag or tag in seen_tags:
                    continue
                seen_tags.add(tag)
                candidates.append(
                    _Candidate(
                        tag=tag,
                        version=_strip_v(tag),
                        url=rel.get("html_url", ""),
                        notes=(rel.get("body") or "")[:3000],
                        prerelease=bool(rel.get("prerelease") or rel.get("draft")),
                    )
                )
    except Exception:
        pass

    # Fill in with bare tags too — plenty of repos never cut a formal
    # "release", only push tags.
    if len(candidates) < _CANDIDATE_LIMIT:
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.get(
                    f"{_GH_API}/repos/{slug}/tags",
                    params={"per_page": _CANDIDATE_LIMIT},
                    headers=headers,
                )
            if resp.status_code == 200:
                for t in resp.json():
                    tag = t.get("name", "")
                    if not tag or tag in seen_tags:
                        continue
                    seen_tags.add(tag)
                    candidates.append(
                        _Candidate(
                            tag=tag,
                            version=_strip_v(tag),
                            url=f"https://github.com/{slug}/releases/tag/{tag}",
                        )
                    )
                    if len(candidates) >= _CANDIDATE_LIMIT:
                        break
        except Exception:
            pass

    return candidates[:_CANDIDATE_LIMIT]


def _gh_slug(source: str) -> str | None:
    """Extract owner/repo from a GitHub URL.

    Handles both bare repo URLs (``.../owner/repo`` or ``.../owner/repo.git``)
    and longer URLs with additional path segments after the repo name, such
    as release-asset download links
    (``.../owner/repo/releases/download/v1.0/asset.tar.gz``).
    """
    m = re.search(r"github\.com[/:]([^/\s]+)/([^/\s.]+)(?:\.git)?(?:/|$)", source)
    return f"{m.group(1)}/{m.group(2)}" if m else None


# ---------------------------------------------------------------------------
# GitLab
# ---------------------------------------------------------------------------

def _gitlab_candidates(source: str, token: str = "") -> list[_Candidate]:
    m = re.search(r"gitlab\.com/(.+?)(?:\.git)?$", source)
    if not m:
        return []
    project = m.group(1).strip("/")
    encoded = project.replace("/", "%2F")
    headers: dict[str, str] = {}
    if token:
        headers["PRIVATE-TOKEN"] = token

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(
                f"https://gitlab.com/api/v4/projects/{encoded}/releases",
                params={"per_page": _CANDIDATE_LIMIT},
                headers=headers,
            )
        if resp.status_code == 200:
            out = []
            for rel in resp.json():
                tag = rel.get("tag_name", "")
                if not tag:
                    continue
                out.append(
                    _Candidate(
                        tag=tag,
                        version=_strip_v(tag),
                        url=rel.get("_links", {}).get("self", ""),
                        notes=(rel.get("description") or "")[:3000],
                        prerelease=bool(rel.get("upcoming_release")),
                    )
                )
            return out
    except Exception:
        pass
    return []


# ---------------------------------------------------------------------------
# PyPI
# ---------------------------------------------------------------------------

def _pypi_candidates(source: str) -> list[_Candidate]:
    # Extract package name from URL or pypi:// URI
    m = re.search(r"pypi\.org/project/([^/]+)", source)
    if not m:
        m = re.match(r"pypi://([^/]+)", source)
    if not m:
        return []
    pkg = m.group(1)
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(f"https://pypi.org/pypi/{pkg}/json")
        if resp.status_code == 200:
            data = resp.json()
            releases = data.get("releases", {})
            project_url = data["info"].get("project_url") or f"https://pypi.org/project/{pkg}/"
            # PyPI doesn't order `releases` — sort by upload time, newest first.
            versions_with_time = []
            for version, files in releases.items():
                if not files:
                    continue
                upload_time = max((f.get("upload_time", "") for f in files), default="")
                versions_with_time.append((upload_time, version))
            versions_with_time.sort(reverse=True)
            candidates = [
                _Candidate(tag=v, version=v, url=project_url)
                for _t, v in versions_with_time[:_CANDIDATE_LIMIT]
            ]
            if not candidates:
                # No releases[] metadata (rare) — fall back to info.version alone.
                version = data["info"]["version"]
                candidates = [_Candidate(tag=version, version=version, url=project_url)]
            return candidates
    except Exception:
        pass
    return []


# ---------------------------------------------------------------------------
# Launchpad
# ---------------------------------------------------------------------------

def _launchpad_candidates(source: str) -> list[_Candidate]:
    # Launchpad uses bazaar/git branches; extract project name and query API
    m = re.search(r"launchpad\.net/([^/\s]+)", source)
    if not m:
        return []
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
                return [_Candidate(tag=version, version=version, url=f"https://launchpad.net/{project}")]
    except Exception:
        pass
    return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_v(tag: str) -> str:
    """Remove leading 'v' or 'V' from a version tag."""
    return tag.lstrip("vV") if tag else tag


def is_newer(latest: str, current: str) -> bool:
    """Return True if latest version string is strictly newer than current.

    Deterministic fallback only used when no local model is reachable — see
    :func:`choose_latest_version`, which is the primary decision path.
    """
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
