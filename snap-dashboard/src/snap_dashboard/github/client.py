"""GitHub and GitLab API client for issues and pull requests."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

_GH_API = "https://api.github.com"
_GL_API = "https://gitlab.com/api/v4"


class GitHubClient:
    """Client for fetching issues and PRs from GitHub and GitLab repos."""

    def __init__(self, token: str = "") -> None:
        self._token = token
        self._gh_headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            self._gh_headers["Authorization"] = f"Bearer {token}"

    def get_open_issues_and_prs(self, repo_url: str) -> list[dict[str, Any]]:
        """Fetch open issues and PRs for the given repository URL.

        Supports github.com and gitlab.com URLs.
        Returns list of dicts with keys:
          issue_number, title, state, type, url, author, created_at, updated_at
        """
        parsed = urlparse(repo_url.rstrip("/"))
        host = parsed.netloc.lower()

        # Extract owner/repo from path like /owner/repo or /owner/repo.git
        path_parts = [p for p in parsed.path.split("/") if p]
        if len(path_parts) < 2:
            logger.warning("Cannot parse repo URL: %s", repo_url)
            return []

        owner = path_parts[0]
        repo = path_parts[1].removesuffix(".git")

        if "github.com" in host:
            return self._fetch_github(owner, repo)
        elif "gitlab.com" in host:
            return self._fetch_gitlab(owner, repo)
        else:
            logger.warning("Unsupported host %r in URL %s", host, repo_url)
            return []

    def _fetch_github(self, owner: str, repo: str) -> list[dict[str, Any]]:
        """Fetch open issues + PRs from GitHub."""
        url = f"{_GH_API}/repos/{owner}/{repo}/issues"
        params = {"state": "open", "per_page": 100}
        results: list[dict[str, Any]] = []

        try:
            with httpx.Client(timeout=30, follow_redirects=True) as client:
                resp = client.get(url, params=params, headers=self._gh_headers)
                if resp.status_code in (401, 403, 404):
                    logger.warning(
                        "GitHub %s/%s returned %s", owner, repo, resp.status_code
                    )
                    return []
                resp.raise_for_status()
                items = resp.json()
        except httpx.HTTPStatusError as exc:
            logger.warning("GitHub HTTP error for %s/%s: %s", owner, repo, exc)
            return []
        except httpx.RequestError as exc:
            logger.warning("GitHub request error for %s/%s: %s", owner, repo, exc)
            return []

        for item in items:
            is_pr = "pull_request" in item
            results.append(
                {
                    "issue_number": item.get("number"),
                    "title": item.get("title", ""),
                    "state": item.get("state", "open"),
                    "type": "pr" if is_pr else "issue",
                    "url": item.get("html_url", ""),
                    "author": (item.get("user") or {}).get("login", ""),
                    "created_at": _parse_dt(item.get("created_at")),
                    "updated_at": _parse_dt(item.get("updated_at")),
                }
            )

        return results

    def _fetch_gitlab(self, owner: str, repo: str) -> list[dict[str, Any]]:
        """Fetch open issues + MRs from GitLab."""
        encoded = f"{owner}%2F{repo}"
        results: list[dict[str, Any]] = []

        headers: dict[str, str] = {}
        if self._token:
            headers["PRIVATE-TOKEN"] = self._token

        # Issues
        try:
            with httpx.Client(timeout=30, follow_redirects=True) as client:
                issues_url = f"{_GL_API}/projects/{encoded}/issues"
                resp = client.get(
                    issues_url,
                    params={"state": "opened", "per_page": 100},
                    headers=headers,
                )
                if resp.status_code in (401, 403, 404):
                    logger.warning(
                        "GitLab %s/%s issues returned %s", owner, repo, resp.status_code
                    )
                else:
                    resp.raise_for_status()
                    for item in resp.json():
                        results.append(
                            {
                                "issue_number": item.get("iid"),
                                "title": item.get("title", ""),
                                "state": "open",
                                "type": "issue",
                                "url": item.get("web_url", ""),
                                "author": (item.get("author") or {}).get(
                                    "username", ""
                                ),
                                "created_at": _parse_dt(item.get("created_at")),
                                "updated_at": _parse_dt(item.get("updated_at")),
                            }
                        )

                # Merge requests
                mrs_url = f"{_GL_API}/projects/{encoded}/merge_requests"
                resp2 = client.get(
                    mrs_url,
                    params={"state": "opened", "per_page": 100},
                    headers=headers,
                )
                if resp2.status_code in (401, 403, 404):
                    logger.warning(
                        "GitLab %s/%s MRs returned %s", owner, repo, resp2.status_code
                    )
                else:
                    resp2.raise_for_status()
                    for item in resp2.json():
                        results.append(
                            {
                                "issue_number": item.get("iid"),
                                "title": item.get("title", ""),
                                "state": "open",
                                "type": "pr",
                                "url": item.get("web_url", ""),
                                "author": (item.get("author") or {}).get(
                                    "username", ""
                                ),
                                "created_at": _parse_dt(item.get("created_at")),
                                "updated_at": _parse_dt(item.get("updated_at")),
                            }
                        )
        except httpx.HTTPStatusError as exc:
            logger.warning("GitLab HTTP error for %s/%s: %s", owner, repo, exc)
        except httpx.RequestError as exc:
            logger.warning("GitLab request error for %s/%s: %s", owner, repo, exc)

        return results


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
