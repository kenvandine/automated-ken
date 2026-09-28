"""Helpers for verifying real npm package versions before proposing bumps.

``UpstreamMaintainerAgent``'s ``dep_update`` task used to just ask the
configured coding backend to "upgrade to the latest compatible versions"
and trust whatever version numbers it wrote into package.json. That's fine
for a backend with real tool use (e.g. the GitHub Copilot cloud agent, which
can actually run ``npm outdated``), but the ``local_lemonade`` backend is a
single-shot, no-tool-use model (see ``lemonade/coding_agent.py``) with no way
to check the npm registry mid-task — it can only guess from its own training
data. In practice this produced a real PR that bumped 7 of 8 npm
dependencies to version numbers that don't exist on npm at all, and
"bumped" the 8th to an *older* version than was already pinned, all of
which then failed CI with a stale/mismatched lockfile.

This module fetches the real current/latest version for each dependency
from the public npm registry so callers can hand the coding backend
verified facts to apply (or skip the task entirely when nothing is
actually outdated), instead of an open-ended "figure it out yourself" task.
"""

from __future__ import annotations

import json
import logging

import httpx
from packaging.version import InvalidVersion, Version

from snap_dashboard.github.bot_client import BotGitHubClient

logger = logging.getLogger(__name__)

_REGISTRY_TIMEOUT = 10.0


def _latest_version(name: str) -> str | None:
    """Return the real npm "latest" dist-tag version for a package, or None
    if it can't be determined (network error, unknown package, etc.) —
    callers should treat None as "can't verify, skip this one" rather than
    guessing.
    """
    try:
        with httpx.Client(timeout=_REGISTRY_TIMEOUT) as client:
            resp = client.get(f"https://registry.npmjs.org/{name}")
        if resp.status_code != 200:
            return None
        return resp.json().get("dist-tags", {}).get("latest")
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        logger.warning("npm_registry: failed to look up %s: %s", name, exc)
        return None


def _strip_range(spec: str) -> str:
    """Strip a leading npm range operator (^, ~, >=, etc.) off a version spec."""
    return spec.lstrip("^~=<> ").strip()


def find_real_outdated_deps(
    client: BotGitHubClient, owner: str, repo: str
) -> dict[str, dict[str, tuple[str, str]]]:
    """Return ``{manifest_path: {package_name: (current_spec, real_latest)}}``
    for npm dependencies that are genuinely outdated, verified against the
    public npm registry — never guessed.

    Skips any package.json under node_modules, and any dependency whose
    current or latest version string isn't a comparable simple version
    (better to silently skip than to risk a false "outdated" or hand a
    hallucinated target version downstream).
    """
    result: dict[str, dict[str, tuple[str, str]]] = {}
    try:
        tree = client.list_tree(owner, repo)
    except Exception as exc:
        logger.warning("npm_registry: failed to list tree for %s/%s: %s", owner, repo, exc)
        return result

    manifest_paths = [
        p for p in tree if p.endswith("package.json") and "node_modules" not in p
    ]
    for path in manifest_paths:
        fetched = client.get_file(owner, repo, path)
        if not fetched:
            continue
        content, _sha = fetched
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            continue

        deps: dict[str, str] = {}
        for key in ("dependencies", "devDependencies"):
            deps.update(data.get(key) or {})

        outdated: dict[str, tuple[str, str]] = {}
        for name, spec in deps.items():
            latest = _latest_version(name)
            if not latest:
                continue
            current = _strip_range(spec)
            try:
                if Version(latest) > Version(current):
                    outdated[name] = (spec, latest)
            except InvalidVersion:
                continue
        if outdated:
            result[path] = outdated

    return result
