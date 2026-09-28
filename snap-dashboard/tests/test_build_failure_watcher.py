"""Tests for BuildFailureWatcherAgent.

Covers: opt-in gating, dedup-by-head-SHA (never re-dispatch for the same
failing commit), a green run producing no dispatch, and the end-to-end
dispatch flow with GitHub/Copilot network calls replaced by fakes.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from snap_dashboard.agents import build_failure_watcher as bfw_module
from snap_dashboard.agents.build_failure_watcher import BuildFailureWatcherAgent
from snap_dashboard.db.models import Base, CopilotTask, Snap, User, UserConfig


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

    monkeypatch.setattr(bfw_module, "get_session", _fake_get_session)
    return session_local


class _FakeDispatcher:
    def __init__(self, task: dict | None = None) -> None:
        self.calls: list[tuple] = []
        self._task = task if task is not None else {"id": "task-1"}

    def start_task(self, owner, repo, prompt, base_ref="main", create_pull_request=True, model=None):
        self.calls.append((owner, repo, prompt))
        return self._task


class _FakeBotClient:
    def __init__(self, run: dict | None = None, jobs: list[dict] | None = None, has_canonical_workflow: bool = True) -> None:
        self._run = run
        self._jobs = jobs or []
        self.has_canonical_workflow = has_canonical_workflow

    def file_exists(self, owner, repo, path):
        return self.has_canonical_workflow

    def get_default_branch(self, owner, repo):
        return "main"

    def latest_workflow_run(self, owner, repo, workflow_file, branch):
        return self._run

    def failed_job_summaries(self, owner, repo, run_id, tail_chars=2000):
        return self._jobs


def _seed(session_local, packaging_repo="kenvandine/neofetch-desktop", enabled=True):
    session = session_local()
    user = User(github_login="kenvandine", github_id=1)
    session.add(user)
    session.flush()
    uc = UserConfig(user_id=user.id, auto_fix_build_failures=enabled, bot_github_token="tok")
    session.add(uc)
    snap = Snap(user_id=user.id, name="neofetch-desktop", packaging_repo=packaging_repo)
    session.add(snap)
    session.commit()
    user_id, snap_id = user.id, snap.id
    session.close()
    return user_id, snap_id


def test_check_snap_skips_when_latest_run_is_green(isolated_session):
    user_id, snap_id = _seed(isolated_session)
    bot_client = _FakeBotClient(run={"id": 1, "conclusion": "success", "head_sha": "abc"})
    dispatcher = _FakeDispatcher()

    agent = BuildFailureWatcherAgent(user_id=user_id)
    did = agent._check_snap(bot_client, dispatcher, user_id, snap_id, "neofetch-desktop", "kenvandine", "neofetch-desktop", None)

    assert did is False
    assert dispatcher.calls == []


def test_check_snap_dispatches_on_failure_and_records_task(isolated_session):
    user_id, snap_id = _seed(isolated_session)
    run = {
        "id": 42,
        "conclusion": "failure",
        "head_sha": "deadbeef",
        "html_url": "https://github.com/kenvandine/neofetch-desktop/actions/runs/42",
    }
    jobs = [
        {
            "name": "build-amd64",
            "url": "https://github.com/kenvandine/neofetch-desktop/actions/runs/42/job/1",
            "log_tail": "cannot validate snap: layout conflict",
        }
    ]
    bot_client = _FakeBotClient(run=run, jobs=jobs)
    dispatcher = _FakeDispatcher()

    agent = BuildFailureWatcherAgent(user_id=user_id)
    did = agent._check_snap(bot_client, dispatcher, user_id, snap_id, "neofetch-desktop", "kenvandine", "neofetch-desktop", None)

    assert did is True
    assert len(dispatcher.calls) == 1
    owner, repo, prompt = dispatcher.calls[0]
    assert (owner, repo) == ("kenvandine", "neofetch-desktop")
    assert "layout conflict" in prompt
    assert "build-amd64" in prompt

    session = isolated_session()
    tasks = session.query(CopilotTask).filter_by(kind="build_fix").all()
    assert len(tasks) == 1
    assert tasks[0].owner_repo == "kenvandine/neofetch-desktop"
    assert tasks[0].dedupe_key == "deadbeef"
    assert tasks[0].status == "queued"
    session.close()


def test_check_snap_does_not_redispatch_for_same_failing_commit(isolated_session):
    user_id, snap_id = _seed(isolated_session)
    session = isolated_session()
    session.add(
        CopilotTask(
            user_id=user_id, snap_id=snap_id, kind="build_fix",
            owner_repo="kenvandine/neofetch-desktop", status="queued", dedupe_key="deadbeef",
        )
    )
    session.commit()
    session.close()

    run = {"id": 42, "conclusion": "failure", "head_sha": "deadbeef"}
    bot_client = _FakeBotClient(run=run, jobs=[])
    dispatcher = _FakeDispatcher()

    agent = BuildFailureWatcherAgent(user_id=user_id)
    did = agent._check_snap(bot_client, dispatcher, user_id, snap_id, "neofetch-desktop", "kenvandine", "neofetch-desktop", None)

    assert did is False
    assert dispatcher.calls == []


def test_check_snap_dispatches_again_for_a_new_failing_commit(isolated_session):
    """A previous fix attempt for an older failing commit must not block a
    dispatch for a *different* (newer) failing commit."""
    user_id, snap_id = _seed(isolated_session)
    session = isolated_session()
    session.add(
        CopilotTask(
            user_id=user_id, snap_id=snap_id, kind="build_fix",
            owner_repo="kenvandine/neofetch-desktop", status="completed", dedupe_key="old-sha",
        )
    )
    session.commit()
    session.close()

    run = {"id": 43, "conclusion": "failure", "head_sha": "new-sha"}
    bot_client = _FakeBotClient(run=run, jobs=[])
    dispatcher = _FakeDispatcher()

    agent = BuildFailureWatcherAgent(user_id=user_id)
    did = agent._check_snap(bot_client, dispatcher, user_id, snap_id, "neofetch-desktop", "kenvandine", "neofetch-desktop", None)

    assert did is True
    assert len(dispatcher.calls) == 1


def test_run_skips_users_without_opt_in(isolated_session, monkeypatch):
    user_id, _ = _seed(isolated_session, enabled=False)
    session = isolated_session()
    uc = session.query(UserConfig).filter_by(user_id=user_id).first()
    session.close()

    monkeypatch.setattr(bfw_module, "get_user_config", lambda uid: uc)
    agent = BuildFailureWatcherAgent(user_id=user_id)
    result = agent._run()

    assert "checked 0 packaging repo(s), dispatched 0 build_fix task(s)" == result


def test_run_dispatches_for_owned_repo_end_to_end(isolated_session, monkeypatch):
    user_id, snap_id = _seed(isolated_session)
    session = isolated_session()
    uc = session.query(UserConfig).filter_by(user_id=user_id).first()
    session.close()

    run = {"id": 42, "conclusion": "failure", "head_sha": "deadbeef", "html_url": "https://example.com/run/42"}
    fake_client = _FakeBotClient(run=run, jobs=[])
    dispatcher = _FakeDispatcher()

    monkeypatch.setattr(bfw_module, "get_user_config", lambda uid: uc)
    monkeypatch.setattr(bfw_module, "get_coding_dispatcher", lambda uc: dispatcher)
    monkeypatch.setattr(bfw_module, "BotGitHubClient", lambda *a, **k: fake_client)

    agent = BuildFailureWatcherAgent(user_id=user_id)
    result = agent._run()

    assert "dispatched 1 build_fix task(s)" in result
    session = isolated_session()
    tasks = session.query(CopilotTask).filter_by(kind="build_fix").all()
    assert len(tasks) == 1
    assert tasks[0].snap_id == snap_id
    session.close()


def test_run_skips_packaging_repo_not_owned_by_user(isolated_session, monkeypatch):
    """A packaging_repo misconfigured to point at a third-party repo must
    never get an automatic build-fix PR opened against it."""
    user_id, _ = _seed(isolated_session, packaging_repo="https://github.com/avojak/warble")
    session = isolated_session()
    uc = session.query(UserConfig).filter_by(user_id=user_id).first()
    session.close()

    run = {"id": 42, "conclusion": "failure", "head_sha": "deadbeef"}
    dispatcher = _FakeDispatcher()

    monkeypatch.setattr(bfw_module, "get_user_config", lambda uid: uc)
    monkeypatch.setattr(bfw_module, "get_coding_dispatcher", lambda uc: dispatcher)
    monkeypatch.setattr(bfw_module, "BotGitHubClient", lambda *a, **k: _FakeBotClient(run=run))

    agent = BuildFailureWatcherAgent(user_id=user_id)
    result = agent._run()

    assert "checked 0 packaging repo(s), dispatched 0 build_fix task(s)" == result
    assert dispatcher.calls == []
