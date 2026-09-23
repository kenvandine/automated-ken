"""Zip up a YARF test suite directory straight from GitHub, in-memory.

Used by the remote-runner API (``GET /api/runners/{id}/jobs/{job_id}/suite``)
so a runner machine never needs its own GitHub credential — the dashboard,
which already holds the user's token, fetches ``suites/<snap>/suite/**``
from ``UserConfig.testing_repo`` and streams back a zip.
"""

from __future__ import annotations

import base64
import io
import logging
import zipfile

import httpx

from snap_dashboard.github.utils import parse_owner_repo

logger = logging.getLogger(__name__)

_GH_API = "https://api.github.com"


def _default_branch(owner: str, repo: str, token: str) -> str:
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(f"{_GH_API}/repos/{owner}/{repo}", headers=headers)
        resp.raise_for_status()
        return resp.json().get("default_branch", "main")
    except httpx.HTTPError:
        return "main"


def list_suite_files(testing_repo: str, snap_name: str, token: str = "") -> dict[str, bytes] | None:
    """Return {relative_path: content_bytes} for ``suites/<snap_name>/suite/`` in ``testing_repo``.

    Paths are relative to the suite directory itself (the ``suites/<snap>/suite/``
    prefix is stripped). Returns None if the repo/branch/suite dir can't be found.
    """
    owner_repo = parse_owner_repo(testing_repo)
    if not owner_repo:
        return None
    owner, repo = owner_repo

    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    branch = _default_branch(owner, repo, token)
    prefix = f"suites/{snap_name}/suite/"

    try:
        with httpx.Client(timeout=30) as client:
            tree_resp = client.get(
                f"{_GH_API}/repos/{owner}/{repo}/git/trees/{branch}",
                params={"recursive": "1"},
                headers=headers,
            )
            tree_resp.raise_for_status()
            entries = tree_resp.json().get("tree", [])

            matches = [
                e for e in entries
                if e.get("type") == "blob" and e.get("path", "").startswith(prefix)
            ]
            if not matches:
                logger.warning("No suite files found under %s in %s/%s", prefix, owner, repo)
                return None

            files: dict[str, bytes] = {}
            for entry in matches:
                blob_resp = client.get(
                    f"{_GH_API}/repos/{owner}/{repo}/git/blobs/{entry['sha']}",
                    headers=headers,
                )
                blob_resp.raise_for_status()
                blob = blob_resp.json()
                content = base64.b64decode(blob["content"]) if blob.get("encoding") == "base64" else b""
                files[entry["path"][len(prefix):]] = content
            return files
    except httpx.HTTPError as exc:
        logger.warning("list_suite_files failed for %s/%s: %s", owner, repo, exc)
        return None


def fetch_suite_zip(testing_repo: str, snap_name: str, token: str = "") -> bytes | None:
    """Return an in-memory zip of ``suites/<snap_name>/suite/`` from ``testing_repo``.

    Returns None (never raises) if the repo, branch, or suite directory
    can't be found/fetched — callers should surface this as a 404 to the
    calling runner.
    """
    files = list_suite_files(testing_repo, snap_name, token)
    if not files:
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, content in files.items():
            zf.writestr(arcname, content)
    return buf.getvalue()
