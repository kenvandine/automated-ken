"""Tests for the local Lemonade coding-task backend.

Covers LocalLemonadeCodingDispatcher.start_task() end-to-end with the
Lemonade client and GitHub network calls replaced by fakes, and the
task_result_fields()/extract_pr_url() helpers shared across coding-backend
callers.
"""

from __future__ import annotations

import json

from snap_dashboard.agents.coding_backend import extract_pr_url, task_result_fields
from snap_dashboard.lemonade import coding_agent as ca_module
from snap_dashboard.lemonade.coding_agent import LocalLemonadeCodingDispatcher


class _FakeLemonadeClient:
    def __init__(self, reply: str | None) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    def is_available(self) -> bool:
        return True

    def chat(self, prompt, system="", temperature=0.2, max_tokens=None, timeout=None):
        self.calls.append(
            {"prompt": prompt, "system": system, "temperature": temperature,
             "max_tokens": max_tokens, "timeout": timeout}
        )
        return self.reply


class _FakeBotClient:
    def __init__(self, tree: list[str] | None = None, files: dict[str, str] | None = None) -> None:
        self._tree = tree or []
        self._files = files or {}

    def get_default_branch(self, owner, repo):
        return "main"

    def list_tree(self, owner, repo, ref=None):
        return self._tree

    def get_file(self, owner, repo, path):
        if path in self._files:
            return self._files[path], "sha"
        return None


class _FakeTreeClient:
    _UNSET = object()

    def __init__(self, commit_ok: bool = True, pr=_UNSET) -> None:
        self.commit_ok = commit_ok
        self.pr = {"html_url": "https://github.com/o/r/pull/1", "number": 1} if pr is self._UNSET else pr
        self.commit_calls: list[dict] = []
        self.pr_calls: list[dict] = []

    def commit_multi(self, owner, repo, branch, message, put_files=None, delete_paths=None, base_branch=None):
        self.commit_calls.append(
            {"owner": owner, "repo": repo, "branch": branch, "message": message,
             "put_files": put_files, "delete_paths": delete_paths, "base_branch": base_branch}
        )
        return branch if self.commit_ok else None

    def create_pr(self, owner, repo, title, body, head, base=None):
        self.pr_calls.append(
            {"owner": owner, "repo": repo, "title": title, "body": body, "head": head, "base": base}
        )
        return self.pr


def _make_dispatcher(monkeypatch, reply, tree=None, files=None, commit_ok=True, pr=_FakeTreeClient._UNSET):
    fake_client = _FakeLemonadeClient(reply)
    monkeypatch.setattr(ca_module, "get_lemonade_client", lambda uc, ensure_started=False, task="text": fake_client)
    dispatcher = LocalLemonadeCodingDispatcher(user_config=object(), token="tok")
    dispatcher._bot = _FakeBotClient(tree=tree, files=files)
    dispatcher._tree = _FakeTreeClient(commit_ok=commit_ok, pr=pr)
    return dispatcher, fake_client


def test_start_task_happy_path_opens_pr(monkeypatch) -> None:
    plan = {
        "commit_message": "chore: normalize workflow",
        "pr_title": "Normalize repo",
        "pr_body": "Automated changes.",
        "files": [
            {"path": "AGENTS.md", "action": "write", "content": "# AGENTS\n"},
            {"path": ".github/workflows/sync-release.yml", "action": "delete"},
        ],
    }
    dispatcher, fake_client = _make_dispatcher(
        monkeypatch, reply=json.dumps(plan), tree=["AGENTS.md", ".github/workflows/sync-release.yml"],
    )

    result = dispatcher.start_task("kenvandine", "my-snap", "normalize this repo")

    assert result["state"] == "completed"
    assert result["html_url"] == "https://github.com/o/r/pull/1"
    assert len(dispatcher._tree.commit_calls) == 1
    commit = dispatcher._tree.commit_calls[0]
    assert commit["put_files"] == {"AGENTS.md": "# AGENTS\n"}
    assert commit["delete_paths"] == [".github/workflows/sync-release.yml"]
    assert len(dispatcher._tree.pr_calls) == 1
    # Coding calls get generous timeout/max_tokens overrides for reliability.
    assert fake_client.calls[0]["max_tokens"] == ca_module._CODING_MAX_TOKENS
    assert fake_client.calls[0]["timeout"] == ca_module._CODING_TIMEOUT_SECONDS


def test_start_task_without_pull_request_skips_pr_open(monkeypatch) -> None:
    plan = {"files": [{"path": "AGENTS.md", "action": "write", "content": "# AGENTS\n"}]}
    dispatcher, _ = _make_dispatcher(monkeypatch, reply=json.dumps(plan))

    result = dispatcher.start_task("o", "r", "task", create_pull_request=False)

    assert result["state"] == "completed"
    assert "html_url" not in result
    assert dispatcher._tree.pr_calls == []


def test_start_task_lemonade_unavailable_returns_failed(monkeypatch) -> None:
    class _Unavailable(_FakeLemonadeClient):
        def is_available(self):
            return False

    monkeypatch.setattr(ca_module, "get_lemonade_client", lambda uc, ensure_started=False, task="text": _Unavailable(None))
    dispatcher = LocalLemonadeCodingDispatcher(user_config=object(), token="tok")

    result = dispatcher.start_task("o", "r", "task")

    assert result["state"] == "failed"
    assert "not reachable" in result["error"]


def test_start_task_unparseable_reply_returns_failed(monkeypatch) -> None:
    dispatcher, _ = _make_dispatcher(monkeypatch, reply="not json at all")

    result = dispatcher.start_task("o", "r", "task")

    assert result["state"] == "failed"


def test_start_task_no_reply_returns_failed(monkeypatch) -> None:
    dispatcher, _ = _make_dispatcher(monkeypatch, reply=None)

    result = dispatcher.start_task("o", "r", "task")

    assert result["state"] == "failed"


def test_start_task_empty_plan_returns_failed(monkeypatch) -> None:
    dispatcher, _ = _make_dispatcher(monkeypatch, reply=json.dumps({"files": []}))

    result = dispatcher.start_task("o", "r", "task")

    assert result["state"] == "failed"
    assert "no file changes" in result["error"]


def test_start_task_commit_failure_returns_failed(monkeypatch) -> None:
    plan = {"files": [{"path": "AGENTS.md", "action": "write", "content": "x"}]}
    dispatcher, _ = _make_dispatcher(monkeypatch, reply=json.dumps(plan), commit_ok=False)

    result = dispatcher.start_task("o", "r", "task")

    assert result["state"] == "failed"
    assert "commit" in result["error"]


def test_start_task_pr_creation_failure_returns_failed(monkeypatch) -> None:
    plan = {"files": [{"path": "AGENTS.md", "action": "write", "content": "x"}]}
    dispatcher, _ = _make_dispatcher(monkeypatch, reply=json.dumps(plan), pr=None)

    result = dispatcher.start_task("o", "r", "task")

    assert result["state"] == "failed"
    assert "pull request" in result["error"]


def test_build_repo_context_skips_binary_and_truncates(monkeypatch) -> None:
    dispatcher, _ = _make_dispatcher(
        monkeypatch,
        reply="{}",
        tree=["README.md", "logo.png", "big.txt"],
        files={"README.md": "hello", "big.txt": "x" * 100},
    )
    monkeypatch.setattr(ca_module, "_MAX_TOTAL_INLINE_CHARS", 10)

    context = dispatcher._build_repo_context("o", "r", "main")

    assert "README.md" in context
    assert "logo.png" in context  # still listed in the file listing
    assert "```\nhello\n```" in context
    assert "omitted for length" in context


# ---------------------------------------------------------------------------
# Shared helpers used by every coding-backend call site.
# ---------------------------------------------------------------------------


def test_task_result_fields_dispatch_failed_for_none() -> None:
    assert task_result_fields(None) == {
        "external_task_id": None, "status": "dispatch_failed", "pr_url": None,
    }


def test_task_result_fields_async_copilot_style() -> None:
    fields = task_result_fields({"id": "task-42"})
    assert fields == {"external_task_id": "task-42", "status": "queued", "pr_url": None}


def test_task_result_fields_sync_local_lemonade_style() -> None:
    fields = task_result_fields({"state": "completed", "html_url": "https://github.com/o/r/pull/9"})
    assert fields == {
        "external_task_id": None, "status": "completed", "pr_url": "https://github.com/o/r/pull/9",
    }


def test_extract_pr_url_handles_multiple_shapes() -> None:
    assert extract_pr_url({"html_url": "https://x/1"}) == "https://x/1"
    assert extract_pr_url({"pull_request_url": "https://x/2"}) == "https://x/2"
    assert extract_pr_url({"pull_request": {"html_url": "https://x/3"}}) == "https://x/3"
    assert extract_pr_url({"pull_request": "https://x/4"}) == "https://x/4"
    assert extract_pr_url({}) is None
