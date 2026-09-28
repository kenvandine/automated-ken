"""Regression tests for PRMonitorAgent watching CI on ``dep_update`` and
``custom_prompt`` PRs.

Before this, ``auto_fix_ci_failures`` only ever looked at ``VersionBumpPR``
rows (snap-packaging-repo version bumps) — a PR opened by
``UpstreamMaintainerAgent``'s ``dep_update`` task against a generic
upstream repo (e.g. kenvandine/obscura#1), or a user-dispatched
``custom_prompt`` task (see agents/custom_prompt.py), had *no* CI
monitoring at all, so the setting appeared to silently do nothing for
those categories of PR. ``PRMonitorAgent._check_dep_update_prs()`` closes
that gap for both kinds.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from snap_dashboard.agents import pr_monitor as prm
from snap_dashboard.db.models import Base, CopilotTask


@pytest.fixture
def isolated_session(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    @contextmanager
    def _fake_get_session():
        session = session_local()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(prm, "get_session", _fake_get_session)
    return session_local


class _FakeResp:
    def __init__(self, status_code: int = 200, data: dict | None = None) -> None:
        self.status_code = status_code
        self._data = data or {}

    def json(self) -> dict:
        return self._data


class _FakeHttpClient:
    """Routes .get(url) by substring match against a fixed response table."""

    def __init__(self, responses: list[tuple[str, "_FakeResp"]]) -> None:
        self._responses = responses

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None):
        for pattern, resp in self._responses:
            if pattern in url:
                return resp
        raise AssertionError(f"unexpected URL in test: {url}")


def _patch_http(monkeypatch, responses: list[tuple[str, "_FakeResp"]]) -> None:
    monkeypatch.setattr(prm.httpx, "Client", lambda *a, **k: _FakeHttpClient(responses))


class _FakeUserConfig:
    def __init__(self, auto_fix_ci_failures: bool = True) -> None:
        self.auto_fix_ci_failures = auto_fix_ci_failures
        self.bot_github_token = "tok"
        self.github_token = "tok"


def _seed_dep_update_task(session_local, ci_status: str | None = None) -> int:
    with session_local() as session:
        task = CopilotTask(
            user_id=1,
            kind="dep_update",
            owner_repo="kenvandine/obscura",
            status="completed",
            pr_url="https://github.com/kenvandine/obscura/pull/1",
            issue_number=1,
            ci_status=ci_status,
        )
        session.add(task)
        session.commit()
        return task.id


class _FakeDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def start_task(self, owner, repo, prompt, base_ref="main", create_pull_request=True, model=None):
        self.calls.append((owner, repo, prompt, base_ref))
        return {"state": "completed", "html_url": "https://github.com/kenvandine/obscura/pull/2"}


def test_dep_update_ci_failure_dispatches_fix_when_opted_in(isolated_session, monkeypatch):
    task_id = _seed_dep_update_task(isolated_session)
    monkeypatch.setattr(prm, "get_user_config", lambda uid: _FakeUserConfig())

    _patch_http(
        monkeypatch,
        [
            ("/pulls/1", _FakeResp(200, {"state": "open", "head": {"sha": "abc123", "ref": "dep-bump-1"}})),
            ("/commits/abc123/check-runs", _FakeResp(200, {"check_runs": [
                {"status": "completed", "conclusion": "failure", "name": "Rust", "html_url": "http://x"},
            ]})),
        ],
    )

    dispatcher = _FakeDispatcher()
    monkeypatch.setattr(prm, "get_coding_dispatcher", lambda uc: dispatcher)

    agent = prm.PRMonitorAgent()
    checked, updated = agent._check_dep_update_prs()

    assert checked == 1
    assert updated == 1
    assert len(dispatcher.calls) == 1
    owner, repo, prompt, base_ref = dispatcher.calls[0]
    assert (owner, repo) == ("kenvandine", "obscura")
    assert "PR #1" in prompt
    # Regression: the fix must stack on top of the failing PR's own branch,
    # not rebase onto main — dispatching against main produced empty-diff
    # fix PRs since the model never saw the original PR's changes at all.
    assert base_ref == "dep-bump-1"

    with isolated_session() as session:
        task = session.query(CopilotTask).get(task_id)
        assert task.ci_status == "ci_failed"
        fix_tasks = session.query(CopilotTask).filter_by(kind="ci_fix").all()
        assert len(fix_tasks) == 1
        assert fix_tasks[0].issue_number == 1
        assert fix_tasks[0].base_ref == "dep-bump-1"


def test_dep_update_ci_failure_respects_opt_out(isolated_session, monkeypatch):
    task_id = _seed_dep_update_task(isolated_session)
    monkeypatch.setattr(prm, "get_user_config", lambda uid: _FakeUserConfig(auto_fix_ci_failures=False))

    _patch_http(
        monkeypatch,
        [
            ("/pulls/1", _FakeResp(200, {"state": "open", "head": {"sha": "abc123"}})),
            ("/commits/abc123/check-runs", _FakeResp(200, {"check_runs": [
                {"status": "completed", "conclusion": "failure", "name": "Rust", "html_url": "http://x"},
            ]})),
        ],
    )

    dispatcher = _FakeDispatcher()
    monkeypatch.setattr(prm, "get_coding_dispatcher", lambda uc: dispatcher)

    agent = prm.PRMonitorAgent()
    agent._check_dep_update_prs()

    assert len(dispatcher.calls) == 0
    with isolated_session() as session:
        task = session.query(CopilotTask).get(task_id)
        assert task.ci_status == "ci_failed"  # still recorded, just no dispatch
        assert session.query(CopilotTask).filter_by(kind="ci_fix").count() == 0


def test_dep_update_pr_closed_stops_watching(isolated_session, monkeypatch):
    task_id = _seed_dep_update_task(isolated_session)
    monkeypatch.setattr(prm, "get_user_config", lambda uid: _FakeUserConfig())
    _patch_http(monkeypatch, [("/pulls/1", _FakeResp(200, {"state": "closed"}))])

    agent = prm.PRMonitorAgent()
    checked, updated = agent._check_dep_update_prs()

    assert checked == 1
    assert updated == 1
    with isolated_session() as session:
        task = session.query(CopilotTask).get(task_id)
        assert task.ci_status == "closed"


def test_dep_update_pr_all_green_marks_passed_without_dispatch(isolated_session, monkeypatch):
    _seed_dep_update_task(isolated_session)
    monkeypatch.setattr(prm, "get_user_config", lambda uid: _FakeUserConfig())
    _patch_http(
        monkeypatch,
        [
            ("/pulls/1", _FakeResp(200, {"state": "open", "head": {"sha": "abc123"}})),
            ("/commits/abc123/check-runs", _FakeResp(200, {"check_runs": [
                {"status": "completed", "conclusion": "success", "name": "Rust"},
            ]})),
        ],
    )
    dispatcher = _FakeDispatcher()
    monkeypatch.setattr(prm, "get_coding_dispatcher", lambda uc: dispatcher)

    agent = prm.PRMonitorAgent()
    checked, updated = agent._check_dep_update_prs()

    assert (checked, updated) == (1, 1)
    assert len(dispatcher.calls) == 0
    with isolated_session() as session:
        task = session.query(CopilotTask).filter_by(owner_repo="kenvandine/obscura").first()
        assert task.ci_status == "ci_passed"


def test_dep_update_prs_already_resolved_are_not_reexamined(isolated_session, monkeypatch):
    _seed_dep_update_task(isolated_session, ci_status="ci_passed")

    def _boom(*a, **k):
        raise AssertionError("should not make any HTTP calls for an already-resolved PR")

    monkeypatch.setattr(prm.httpx, "Client", _boom)

    agent = prm.PRMonitorAgent()
    checked, updated = agent._check_dep_update_prs()

    assert (checked, updated) == (0, 0)


def test_custom_prompt_ci_failure_dispatches_fix_when_opted_in(isolated_session, monkeypatch):
    """A user-dispatched ``custom_prompt`` PR (see agents/custom_prompt.py)
    gets the same generic CI-watch/fix-dispatch treatment as a dep_update PR.
    """
    with isolated_session() as session:
        task = CopilotTask(
            user_id=1,
            kind="custom_prompt",
            owner_repo="kenvandine/neofetch-desktop",
            status="completed",
            pr_url="https://github.com/kenvandine/neofetch-desktop/pull/3",
            issue_number=3,
        )
        session.add(task)
        session.commit()
        task_id = task.id

    monkeypatch.setattr(prm, "get_user_config", lambda uid: _FakeUserConfig())
    _patch_http(
        monkeypatch,
        [
            ("/pulls/3", _FakeResp(200, {"state": "open", "head": {"sha": "def456", "ref": "lemonade-coding/1"}})),
            ("/commits/def456/check-runs", _FakeResp(200, {"check_runs": [
                {"status": "completed", "conclusion": "failure", "name": "snap", "html_url": "http://x"},
            ]})),
        ],
    )

    dispatcher = _FakeDispatcher()
    monkeypatch.setattr(prm, "get_coding_dispatcher", lambda uc: dispatcher)

    agent = prm.PRMonitorAgent()
    checked, updated = agent._check_dep_update_prs()

    assert checked == 1
    assert updated == 1
    assert len(dispatcher.calls) == 1
    owner, repo, prompt, base_ref = dispatcher.calls[0]
    assert (owner, repo) == ("kenvandine", "neofetch-desktop")
    assert "user-typed custom task" in prompt
    assert base_ref == "lemonade-coding/1"

    with isolated_session() as session:
        task = session.query(CopilotTask).get(task_id)
        assert task.ci_status == "ci_failed"
        fix_tasks = session.query(CopilotTask).filter_by(kind="ci_fix").all()
        assert len(fix_tasks) == 1
        assert fix_tasks[0].issue_number == 3
