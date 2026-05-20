"""GitHub bot-account client for creating version-bump PRs.

Uses the GitHub Contents API (no git clone required) to:
  1. Read the current snapcraft.yaml + its SHA
  2. Write an updated version to a new branch
  3. Open a PR from the bot account
"""

from __future__ import annotations

import base64
import logging
import re

import httpx

logger = logging.getLogger(__name__)

_GH_API = "https://api.github.com"


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }


class BotGitHubClient:
    """Creates version-bump branches and PRs using a bot GitHub account."""

    def __init__(self, token: str) -> None:
        self.token = token

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_file(self, owner: str, repo: str, path: str) -> tuple[str, str] | None:
        """Return (content_text, sha) for a file, or None."""
        url = f"{_GH_API}/repos/{owner}/{repo}/contents/{path}"
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.get(url, headers=_headers(self.token))
            if resp.status_code != 200:
                return None
            data = resp.json()
            content = base64.b64decode(data["content"]).decode()
            return content, data["sha"]
        except Exception as exc:
            logger.warning("get_file %s/%s/%s failed: %s", owner, repo, path, exc)
            return None

    def get_default_branch(self, owner: str, repo: str) -> str:
        """Return the repo's default branch name (usually 'main' or 'master')."""
        url = f"{_GH_API}/repos/{owner}/{repo}"
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.get(url, headers=_headers(self.token))
            if resp.status_code == 200:
                return resp.json().get("default_branch", "main")
        except Exception:
            pass
        return "main"

    def get_branch_sha(self, owner: str, repo: str, branch: str) -> str | None:
        """Return the HEAD commit SHA of a branch."""
        url = f"{_GH_API}/repos/{owner}/{repo}/git/ref/heads/{branch}"
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.get(url, headers=_headers(self.token))
            if resp.status_code == 200:
                return resp.json()["object"]["sha"]
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def create_branch(self, owner: str, repo: str, branch: str, from_sha: str) -> bool:
        """Create a new branch pointing at from_sha."""
        url = f"{_GH_API}/repos/{owner}/{repo}/git/refs"
        payload = {"ref": f"refs/heads/{branch}", "sha": from_sha}
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.post(url, json=payload, headers=_headers(self.token))
            return resp.status_code in (200, 201)
        except Exception as exc:
            logger.warning("create_branch %s failed: %s", branch, exc)
            return False

    def update_file(
        self,
        owner: str,
        repo: str,
        path: str,
        content: str,
        sha: str,
        branch: str,
        commit_message: str,
    ) -> bool:
        """Commit an updated file to an existing branch."""
        url = f"{_GH_API}/repos/{owner}/{repo}/contents/{path}"
        payload = {
            "message": commit_message,
            "content": base64.b64encode(content.encode()).decode(),
            "sha": sha,
            "branch": branch,
        }
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.put(url, json=payload, headers=_headers(self.token))
            return resp.status_code in (200, 201)
        except Exception as exc:
            logger.warning("update_file %s failed: %s", path, exc)
            return False

    def create_pr(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> dict | None:
        """Open a pull request; return the PR dict or None."""
        url = f"{_GH_API}/repos/{owner}/{repo}/pulls"
        payload = {"title": title, "body": body, "head": head, "base": base}
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.post(url, json=payload, headers=_headers(self.token))
            if resp.status_code in (200, 201):
                return resp.json()
            logger.warning("create_pr failed %s: %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            logger.warning("create_pr failed: %s", exc)
        return None

    def file_exists(self, owner: str, repo: str, path: str) -> bool:
        """Return True if the file exists in the repo's default branch."""
        url = f"{_GH_API}/repos/{owner}/{repo}/contents/{path}"
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.head(url, headers=_headers(self.token))
            return resp.status_code == 200
        except Exception:
            return False

    def create_file(
        self,
        owner: str,
        repo: str,
        path: str,
        content: str,
        commit_message: str,
        branch: str = "main",
    ) -> bool:
        """Create a new file in the repo (fails if the file already exists)."""
        url = f"{_GH_API}/repos/{owner}/{repo}/contents/{path}"
        payload = {
            "message": commit_message,
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch,
        }
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.put(url, json=payload, headers=_headers(self.token))
            return resp.status_code in (200, 201)
        except Exception as exc:
            logger.warning("create_file %s failed: %s", path, exc)
            return False

    def dispatch_workflow(
        self,
        owner: str,
        repo: str,
        workflow_file: str,
        ref: str = "main",
        inputs: dict | None = None,
    ) -> tuple[bool, str]:
        """Dispatch a workflow_dispatch event.

        Returns (success, error_message).
        """
        url = f"{_GH_API}/repos/{owner}/{repo}/actions/workflows/{workflow_file}/dispatches"
        payload: dict = {"ref": ref}
        if inputs:
            payload["inputs"] = inputs
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.post(url, json=payload, headers=_headers(self.token))
            if resp.status_code == 204:
                return True, ""
            return False, f"GitHub API returned {resp.status_code}: {resp.text[:300]}"
        except Exception as exc:
            return False, str(exc)

    def branch_exists(self, owner: str, repo: str, branch: str) -> bool:
        url = f"{_GH_API}/repos/{owner}/{repo}/git/ref/heads/{branch}"
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.get(url, headers=_headers(self.token))
            return resp.status_code == 200
        except Exception:
            return False


# ---------------------------------------------------------------------------
# snapcraft.yaml patching
# ---------------------------------------------------------------------------

_SNAPCRAFT_PATHS = [
    "snap/snapcraft.yaml",
    "snapcraft.yaml",
    ".snapcraft.yaml",
]


def find_snapcraft_yaml(client: BotGitHubClient, owner: str, repo: str) -> tuple[str, str, str] | None:
    """Return (path, content, sha) for the first snapcraft.yaml found."""
    for path in _SNAPCRAFT_PATHS:
        result = client.get_file(owner, repo, path)
        if result:
            content, sha = result
            return path, content, sha
    return None


def patch_snapcraft_yaml(content: str, part_name: str, new_version: str) -> str:
    """Update source-tag (and optionally top-level version) in snapcraft.yaml text.

    This is a targeted line-by-line patch that preserves all other formatting.
    """
    lines = content.splitlines(keepends=True)
    in_target_part = False
    in_parts_section = False
    result = []

    # Determine indentation style of the file
    tag_prefix = "    source-tag:"  # 4-space default

    for i, line in enumerate(lines):
        stripped = line.strip()

        if stripped == "parts:":
            in_parts_section = True
            result.append(line)
            continue

        if in_parts_section:
            # Detect part headers (2-space indent + name + colon)
            if re.match(r"^  [A-Za-z0-9_\-]+:\s*$", line):
                in_target_part = stripped.rstrip(":") == part_name
                result.append(line)
                continue

            if in_target_part and re.match(r"^    source-tag:", line):
                # Preserve quoting style
                quote = "'"
                if '"' in line:
                    quote = '"'
                elif "'" not in line and not line.strip().endswith(":"):
                    quote = ""
                indent = re.match(r"^(\s+)", line)
                indent_str = indent.group(1) if indent else "    "
                if quote:
                    result.append(f"{indent_str}source-tag: {quote}{new_version}{quote}\n")
                else:
                    result.append(f"{indent_str}source-tag: {new_version}\n")
                continue

        result.append(line)

    # Also bump top-level `version:` if it looks like a version string
    patched = "".join(result)
    patched = re.sub(
        r"^(version:\s*)['\"]?[^\s'\"#]+['\"]?",
        lambda m: m.group(1) + new_version,
        patched,
        count=1,
        flags=re.MULTILINE,
    )
    return patched
