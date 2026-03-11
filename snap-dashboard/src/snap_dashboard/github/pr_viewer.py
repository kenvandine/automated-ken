"""Fetch and parse test result PRs from the testing repository."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_GH_API = "https://api.github.com"


def _gh_headers(token: str) -> dict[str, str]:
    """Return GitHub API request headers, with auth if a token is provided."""
    h: dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def get_test_prs(testing_repo: str, token: str = "") -> list[dict[str, Any]]:
    """Fetch open PRs labeled ``snap-test-results`` from *testing_repo*.

    Args:
        testing_repo: Repository slug in ``owner/repo`` format.
        token: Optional GitHub personal access token.

    Returns:
        A list of PR dicts as returned by the GitHub REST API, filtered to only
        those that carry the ``snap-test-results`` label.
    """
    owner, _, repo = testing_repo.partition("/")
    if not repo:
        logger.warning("get_test_prs: invalid testing_repo %r", testing_repo)
        return []

    url = f"{_GH_API}/repos/{owner}/{repo}/pulls"
    params: dict[str, Any] = {"state": "open", "per_page": 100}
    try:
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            resp = client.get(url, params=params, headers=_gh_headers(token))
            if resp.status_code in (401, 403, 404):
                logger.warning(
                    "get_test_prs: GitHub returned %s for %s", resp.status_code, testing_repo
                )
                return []
            resp.raise_for_status()
            prs: list[dict] = resp.json()

        # Filter to PRs that carry the snap-test-results label
        return [
            pr
            for pr in prs
            if "snap-test-results" in {label.get("name", "") for label in pr.get("labels", [])}
        ]
    except httpx.RequestError as exc:
        logger.warning("get_test_prs: request error: %s", exc)
        return []


def get_pr_details(
    testing_repo: str,
    pr_number: int,
    token: str = "",
) -> dict[str, Any]:
    """Fetch full PR details including body, changed files, and comments.

    Returns a dict with keys:
        ``pr``       – full PR object from GitHub API
        ``metadata`` – parsed :func:`parse_pr_metadata` dict
        ``files``    – list of changed-file objects
        ``comments`` – list of issue comment objects
    """
    owner, _, repo = testing_repo.partition("/")
    if not repo:
        return {}

    headers = _gh_headers(token)
    result: dict[str, Any] = {}

    try:
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            # Fetch the PR itself
            pr_resp = client.get(
                f"{_GH_API}/repos/{owner}/{repo}/pulls/{pr_number}",
                headers=headers,
            )
            if pr_resp.status_code != 200:
                logger.warning(
                    "get_pr_details: PR #%s returned %s", pr_number, pr_resp.status_code
                )
                return {}
            pr = pr_resp.json()
            result["pr"] = pr
            result["metadata"] = parse_pr_metadata(pr.get("body", ""))

            # Changed files (to locate screenshots in results/)
            files_resp = client.get(
                f"{_GH_API}/repos/{owner}/{repo}/pulls/{pr_number}/files",
                headers=headers,
                params={"per_page": 100},
            )
            result["files"] = files_resp.json() if files_resp.status_code == 200 else []

            # Issue comments (PR timeline comments)
            comments_resp = client.get(
                f"{_GH_API}/repos/{owner}/{repo}/issues/{pr_number}/comments",
                headers=headers,
                params={"per_page": 50},
            )
            result["comments"] = (
                comments_resp.json() if comments_resp.status_code == 200 else []
            )

    except httpx.RequestError as exc:
        logger.warning("get_pr_details: request error for PR #%s: %s", pr_number, exc)

    return result


def parse_pr_metadata(body: str) -> dict[str, str]:
    """Parse the ``<!-- snap-test-metadata ... -->`` block embedded in a PR body.

    The block format expected in the PR body is::

        <!-- snap-test-metadata
        snap: firefox
        version: 123.0
        from_channel: candidate
        revision: 42
        dashboard_run_id: 7
        yarf_exit_code: 0
        gh_run_id: 9876543210
        status: passed
        -->

    Returns:
        A ``dict[str, str]`` of the key/value pairs inside the metadata block.
        Returns an empty dict if no block is found.
    """
    if not body:
        return {}
    match = re.search(
        r"<!--\s*snap-test-metadata\s*\n(.*?)\s*-->",
        body,
        re.DOTALL,
    )
    if not match:
        return {}
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta


def get_pr_screenshot_urls(
    testing_repo: str,
    pr_data: dict[str, Any],
    branch: str,  # kept for API compatibility; we use head SHA instead
    token: str = "",
) -> list[str]:
    """Return raw GitHub URLs for ``.png`` files in the ``results/`` directory of the PR.

    Args:
        testing_repo: Repository slug in ``owner/repo`` format.
        pr_data: The full dict returned by :func:`get_pr_details`.
        branch: Unused; kept for signature compatibility.
        token: Optional GitHub personal access token (not needed for raw URLs).

    Returns:
        A list of raw content URLs suitable for use in ``<img>`` tags.
    """
    owner, _, repo = testing_repo.partition("/")
    if not repo:
        return []

    head_sha = pr_data.get("pr", {}).get("head", {}).get("sha", "")
    if not head_sha:
        return []

    urls: list[str] = []
    for f in pr_data.get("files", []):
        fname = f.get("filename", "")
        if fname.lower().endswith(".png") and "results/" in fname:
            raw_url = (
                f"https://raw.githubusercontent.com/{owner}/{repo}/{head_sha}/{fname}"
            )
            urls.append(raw_url)

    return urls
