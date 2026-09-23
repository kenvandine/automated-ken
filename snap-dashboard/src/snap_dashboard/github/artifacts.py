"""Download GitHub Actions workflow-run artifacts (used for YARF screenshot ingestion).

The deployed ``snap-test.yml`` workflow uploads its results directory via
``actions/upload-artifact`` rather than committing screenshots to a branch/PR.
This module fetches that artifact zip via the Actions API and extracts the
PNG screenshots from it — no git clone or branch involved.
"""

from __future__ import annotations

import io
import logging
import zipfile

import httpx

logger = logging.getLogger(__name__)

_GH_API = "https://api.github.com"


def _headers(token: str) -> dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def list_run_artifacts(owner: str, repo: str, gh_run_id: str, token: str) -> list[dict]:
    """Return the artifact metadata list for a completed workflow run."""
    url = f"{_GH_API}/repos/{owner}/{repo}/actions/runs/{gh_run_id}/artifacts"
    with httpx.Client(timeout=30) as client:
        resp = client.get(url, headers=_headers(token))
    resp.raise_for_status()
    return resp.json().get("artifacts", [])


def download_artifact_zip(archive_download_url: str, token: str) -> bytes:
    """Download an artifact's zip archive. Requires a token (artifact API is auth-only)."""
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        resp = client.get(archive_download_url, headers=_headers(token))
    resp.raise_for_status()
    return resp.content


def extract_pngs(zip_bytes: bytes) -> list[tuple[str, bytes]]:
    """Return ``(filename, bytes)`` for every PNG entry in a zip archive."""
    out: list[tuple[str, bytes]] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            if name.lower().endswith(".png"):
                out.append((name.rsplit("/", 1)[-1], zf.read(name)))
    return out


def fetch_run_screenshots(
    owner: str,
    repo: str,
    gh_run_id: str,
    token: str,
    name_prefix: str = "yarf-results",
) -> list[tuple[str, bytes]]:
    """Fetch and extract all PNG screenshots from a workflow run's artifacts.

    Returns an empty list (rather than raising) on any failure — screenshot
    ingestion is best-effort and must never break test-status polling.
    """
    if not gh_run_id or not token:
        return []
    try:
        artifacts = list_run_artifacts(owner, repo, gh_run_id, token)
    except httpx.HTTPError as exc:
        logger.warning("fetch_run_screenshots: failed to list artifacts for run %s: %s", gh_run_id, exc)
        return []

    results: list[tuple[str, bytes]] = []
    for artifact in artifacts:
        if name_prefix not in artifact.get("name", ""):
            continue
        if artifact.get("expired"):
            continue
        download_url = artifact.get("archive_download_url")
        if not download_url:
            continue
        try:
            zip_bytes = download_artifact_zip(download_url, token)
            results.extend(extract_pngs(zip_bytes))
        except (httpx.HTTPError, zipfile.BadZipFile) as exc:
            logger.warning(
                "fetch_run_screenshots: failed to download/extract artifact %s (run %s): %s",
                artifact.get("name"), gh_run_id, exc,
            )
    return results
