"""Multi-file atomic commits via the Git Data API.

The Contents API (used by ``bot_client.BotGitHubClient``) only supports one
file per commit, which is fine for a version bump (one file) but not for
"add these 12 test files, delete this workflow, add a doc" in a single PR.
This module builds one commit with an arbitrary set of file writes/deletes
using the lower-level git/trees + git/commits + git/refs endpoints.

Not currently used by any agent — the fleet-normalization campaign
(``agents/repo_normalizer.py``) now delegates that kind of multi-file work
to GitHub Copilot cloud agent instead of committing files mechanically
(Copilot can inspect the actual repo, decide *how* to remove a workflow, and
write its own AGENTS.md, which this module can't do). Kept as reusable infra
for any future *purely mechanical* multi-file commit that doesn't need a
coding model's judgment.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

_GH_API = "https://api.github.com"


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }


class GitTreeClient:
    """Builds one multi-file commit + branch from a set of puts/deletes."""

    def __init__(self, token: str) -> None:
        self.token = token

    def commit_multi(
        self,
        owner: str,
        repo: str,
        branch: str,
        message: str,
        put_files: dict[str, str | bytes] | None = None,
        delete_paths: list[str] | None = None,
        base_branch: str | None = None,
    ) -> str | None:
        """Create (or fast-forward) ``branch`` with one commit containing all
        the given file writes/deletes, based on ``base_branch`` (default branch
        if unset). Returns the branch name on success, else None.
        """
        put_files = put_files or {}
        delete_paths = delete_paths or []
        if not put_files and not delete_paths:
            return None

        try:
            with httpx.Client(timeout=30) as client:
                headers = _headers(self.token)

                base = base_branch or self._default_branch(client, owner, repo, headers)
                base_ref = client.get(
                    f"{_GH_API}/repos/{owner}/{repo}/git/ref/heads/{base}",
                    headers=headers,
                )
                base_ref.raise_for_status()
                base_commit_sha = base_ref.json()["object"]["sha"]

                base_commit = client.get(
                    f"{_GH_API}/repos/{owner}/{repo}/git/commits/{base_commit_sha}",
                    headers=headers,
                )
                base_commit.raise_for_status()
                base_tree_sha = base_commit.json()["tree"]["sha"]

                tree_entries = []
                for path, content in put_files.items():
                    blob_content = content.decode() if isinstance(content, bytes) else content
                    blob_resp = client.post(
                        f"{_GH_API}/repos/{owner}/{repo}/git/blobs",
                        json={"content": blob_content, "encoding": "utf-8"},
                        headers=headers,
                    )
                    blob_resp.raise_for_status()
                    tree_entries.append(
                        {
                            "path": path,
                            "mode": "100644",
                            "type": "blob",
                            "sha": blob_resp.json()["sha"],
                        }
                    )
                for path in delete_paths:
                    tree_entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})

                tree_resp = client.post(
                    f"{_GH_API}/repos/{owner}/{repo}/git/trees",
                    json={"base_tree": base_tree_sha, "tree": tree_entries},
                    headers=headers,
                )
                tree_resp.raise_for_status()
                new_tree_sha = tree_resp.json()["sha"]

                commit_resp = client.post(
                    f"{_GH_API}/repos/{owner}/{repo}/git/commits",
                    json={"message": message, "tree": new_tree_sha, "parents": [base_commit_sha]},
                    headers=headers,
                )
                commit_resp.raise_for_status()
                new_commit_sha = commit_resp.json()["sha"]

                ref_check = client.get(
                    f"{_GH_API}/repos/{owner}/{repo}/git/ref/heads/{branch}",
                    headers=headers,
                )
                if ref_check.status_code == 200:
                    update_resp = client.patch(
                        f"{_GH_API}/repos/{owner}/{repo}/git/refs/heads/{branch}",
                        json={"sha": new_commit_sha, "force": True},
                        headers=headers,
                    )
                    update_resp.raise_for_status()
                else:
                    create_resp = client.post(
                        f"{_GH_API}/repos/{owner}/{repo}/git/refs",
                        json={"ref": f"refs/heads/{branch}", "sha": new_commit_sha},
                        headers=headers,
                    )
                    create_resp.raise_for_status()

                return branch
        except httpx.HTTPError as exc:
            logger.warning("commit_multi failed for %s/%s branch=%s: %s", owner, repo, branch, exc)
            return None

    def create_pr(
        self, owner: str, repo: str, title: str, body: str, head: str, base: str | None = None,
    ) -> dict | None:
        try:
            with httpx.Client(timeout=15) as client:
                headers = _headers(self.token)
                pr_base = base or self._default_branch(client, owner, repo, headers)
                resp = client.post(
                    f"{_GH_API}/repos/{owner}/{repo}/pulls",
                    json={"title": title, "body": body, "head": head, "base": pr_base},
                    headers=headers,
                )
            if resp.status_code in (200, 201):
                return resp.json()
            logger.warning("create_pr failed %s: %s", resp.status_code, resp.text[:300])
        except httpx.HTTPError as exc:
            logger.warning("create_pr failed: %s", exc)
        return None

    def _default_branch(self, client: httpx.Client, owner: str, repo: str, headers: dict) -> str:
        resp = client.get(f"{_GH_API}/repos/{owner}/{repo}", headers=headers)
        resp.raise_for_status()
        return resp.json().get("default_branch", "main")
