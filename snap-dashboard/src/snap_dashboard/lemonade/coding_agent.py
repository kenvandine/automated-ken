"""Local Lemonade coding-task backend — the ``local_lemonade`` CodingDispatcher.

Unlike ``CopilotAgentClient`` (which kicks off an async GitHub Copilot cloud
agent task and is polled for a PR later — see ``github/copilot_agent.py``),
this does the whole thing synchronously in a single ``start_task()`` call,
using only the GitHub REST API (no git clone/push needed):

  1. List the repo's file tree and inline a bounded set of text files as
     context (skipping binaries/lockfiles/build artifacts).
  2. Ask the local Lemonade "coding" model (Qwen3-Coder — see
     ``lemonade.models.TASK_CODING``, loaded with a generous context window,
     see ``lemonade.models.TASK_CONTEXT_SIZES``) for a strict-JSON plan of
     files to write/delete plus a commit message and PR title/body.
  3. Apply that plan as one atomic multi-file commit via ``GitTreeClient``
     (``github/tree_commit.py``), and open the PR.

This is necessarily less capable than GitHub Copilot cloud agent — no
multi-turn tool use, no running tests, no iterating on CI failures. It's a
best-effort single-shot patch, offered as a local/no-subscription-required
option for the same "capable coding" call sites (``agents/pr_monitor.py``,
``agents/upstream_maintainer.py``, ``agents/repo_normalizer.py``), all of
which go through ``agents/coding_backend.get_coding_dispatcher()``.
"""

from __future__ import annotations

import json
import logging
import time

from snap_dashboard.github.bot_client import BotGitHubClient
from snap_dashboard.github.tree_commit import GitTreeClient
from snap_dashboard.lemonade.client import LemonadeClient, get_lemonade_client
from snap_dashboard.lemonade.models import TASK_CODING

logger = logging.getLogger(__name__)

# Skip inlining paths that are almost certainly binary/generated/huge —
# keeps the prompt inside the coding model's context window and avoids
# wasting tokens on content the model can't usefully act on anyway.
_SKIP_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".webp",
    ".woff", ".woff2", ".ttf", ".eot",
    ".zip", ".tar", ".gz", ".xz", ".snap", ".whl",
    ".lock", ".lockb", "-lock.json", ".min.js", ".map",
)
_MAX_FILES_INLINED = 25
_MAX_TOTAL_INLINE_CHARS = 60_000
_MAX_LISTED_PATHS = 300

# Local generation is slower and the reply can be a lot longer than a PR
# description (whole-file rewrites) — give both plenty of headroom rather
# than truncating a cold, multi-gigabyte-model, big-context request.
_CODING_TIMEOUT_SECONDS = 1200
_CODING_MAX_TOKENS = 8000


class LocalLemonadeCodingDispatcher:
    """Implements the ``CodingDispatcher`` protocol using a local Lemonade model."""

    def __init__(self, user_config, token: str) -> None:
        self._uc = user_config
        self.token = token
        # Reads (repo contents, trees, branch refs) go through the repo
        # owner's own token when configured — it's reliable for repos the
        # owner has always had full access to, whereas the bot account may
        # not even be a collaborator yet. Writes (commits/PRs) still use
        # ``token`` (the bot account) so authorship stays consistent.
        read_token = getattr(user_config, "github_token", "") or token
        bot_login = getattr(user_config, "bot_github_login", None)
        self._bot = BotGitHubClient(token, bot_login=bot_login, read_token=read_token)
        self._tree = GitTreeClient(token, bot_login=bot_login, read_token=read_token)

    def start_task(
        self,
        owner: str,
        repo: str,
        prompt: str,
        base_ref: str = "main",
        create_pull_request: bool = True,
        model: str | None = None,
    ) -> dict | None:
        client = get_lemonade_client(self._uc, ensure_started=True, task=TASK_CODING)
        if client is None or not client.is_available():
            logger.warning(
                "local_lemonade coding backend: Lemonade server unavailable for %s/%s", owner, repo,
            )
            return {"state": "failed", "error": "Local Lemonade server is not reachable"}

        base_branch = base_ref or self._bot.get_default_branch(owner, repo)
        context = self._build_repo_context(owner, repo, base_branch)
        plan = self._request_plan(client, owner, repo, prompt, context)
        if not plan:
            return {"state": "failed", "error": "local coding model did not return a usable plan"}

        put_files = {
            f["path"]: f.get("content", "")
            for f in plan.get("files", [])
            if f.get("path") and f.get("action", "write") != "delete"
        }
        delete_paths = [
            f["path"] for f in plan.get("files", []) if f.get("path") and f.get("action") == "delete"
        ]
        if not put_files and not delete_paths:
            return {"state": "failed", "error": "local coding model produced no file changes"}

        branch = f"lemonade-coding/{int(time.time())}"
        commit_message = plan.get("commit_message") or "chore: automated-ken local coding task"
        created_branch = self._tree.commit_multi(
            owner, repo, branch, commit_message,
            put_files=put_files, delete_paths=delete_paths, base_branch=base_branch,
        )
        if not created_branch:
            return {"state": "failed", "error": "failed to commit changes to a new branch"}

        if not create_pull_request:
            return {"state": "completed", "branch": branch}

        pr = self._tree.create_pr(
            owner, repo,
            title=plan.get("pr_title") or commit_message,
            body=plan.get("pr_body") or "Opened automatically by automated-ken's local Lemonade coding backend.",
            head=branch, base=base_branch,
        )
        if not pr:
            return {"state": "failed", "error": "committed changes but failed to open a pull request"}
        return {"state": "completed", "html_url": pr.get("html_url"), "number": pr.get("number")}

    def _build_repo_context(self, owner: str, repo: str, branch: str) -> str:
        paths = self._bot.list_tree(owner, repo, ref=branch)
        inlinable = [p for p in paths if not p.lower().endswith(_SKIP_SUFFIXES)]

        blocks: list[str] = []
        total = 0
        for path in inlinable[:_MAX_FILES_INLINED]:
            result = self._bot.get_file(owner, repo, path)
            if not result:
                continue
            content, _sha = result
            total += len(content)
            if total > _MAX_TOTAL_INLINE_CHARS:
                blocks.append("(remaining file contents omitted for length)")
                break
            blocks.append(f"### {path}\n```\n{content}\n```")

        listing = "\n".join(f"- {p}" for p in paths[:_MAX_LISTED_PATHS])
        return f"Full file listing:\n{listing}\n\nFile contents:\n\n" + "\n\n".join(blocks)

    def _request_plan(
        self, client: LemonadeClient, owner: str, repo: str, prompt: str, context: str,
    ) -> dict | None:
        system = (
            "You are a careful coding agent operating on a single GitHub repository. "
            "You are given a task and the repository's current file tree/contents. "
            "Respond with ONLY a single JSON object (no markdown code fences, no "
            "commentary before or after it) in this exact shape:\n"
            '{"commit_message": "...", "pr_title": "...", "pr_body": "...", '
            '"files": [{"path": "relative/path", "action": "write|delete", '
            '"content": "full new file content, omit for delete"}]}\n\n'
            'For "write" actions, always include the FULL new content of the file, '
            "never a diff/patch fragment. Only include files that actually need to "
            'change. If nothing needs to change, return {"files": []}.'
        )
        user_msg = f"Repository: {owner}/{repo}\n\nTask:\n{prompt}\n\n{context}"
        reply = client.chat(
            user_msg, system=system, temperature=0.1,
            max_tokens=_CODING_MAX_TOKENS, timeout=_CODING_TIMEOUT_SECONDS,
        )
        if not reply:
            return None
        return _parse_plan(reply)


def _parse_plan(reply: str) -> dict | None:
    try:
        start = reply.find("{")
        end = reply.rfind("}") + 1
        if start == -1 or end == 0:
            return None
        data = json.loads(reply[start:end])
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.warning("local_lemonade coding backend: failed to parse model plan: %s", exc)
        return None
