"""Tests for StackUpdateAgent.

Exercises the single-snap "Check for Stack Updates" dispatch against an
isolated in-memory database, with GitHub/Copilot network calls replaced by
fakes.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from snap_dashboard.agents import stack_updater as su_module
from snap_dashboard.agents.stack_updater import StackUpdateAgent
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

    monkeypatch.setattr(su_module, "get_session", _fake_get_session)
    return session_local


class _FakeDispatcher:
    def __init__(self, task: dict | None = None) -> None:
        self.calls: list[tuple] = []
        self.last_error = None
        self._task = task if task is not None else {"id": "task-1"}

    def start_task(self, owner, repo, prompt, base_ref="main", create_pull_request=True, model=None):
        self.calls.append((owner, repo, prompt))
        return self._task


class _FakeBotClient:
    def __init__(self, *a, **k) -> None:
        pass

    def get_default_branch(self, owner, repo):
        return "main"


def _seed(session_local, packaging_repo="kenvandine/my-electron-app"):
    session = session_local()
    user = User(github_login="kenvandine", github_id=1)
    session.add(user)
    session.flush()
    uc = UserConfig(user_id=user.id, bot_github_token="tok")
    session.add(uc)
    snap = Snap(
        user_id=user.id,
        name="my-electron-app",
        packaging_repo=packaging_repo,
        upstream_repo=packaging_repo,
    )
    session.add(snap)
    session.commit()
    user_id, snap_id = user.id, snap.id
    session.close()
    return user_id, snap_id


def test_dispatches_stack_update_and_records_task(isolated_session, monkeypatch) -> None:
    user_id, snap_id = _seed(isolated_session)
    session = isolated_session()
    uc = session.query(UserConfig).filter_by(user_id=user_id).first()
    session.close()

    dispatcher = _FakeDispatcher()
    monkeypatch.setattr(su_module, "get_user_config", lambda uid: uc)
    monkeypatch.setattr(su_module, "get_coding_dispatcher", lambda uc: dispatcher)
    monkeypatch.setattr(su_module, "BotGitHubClient", _FakeBotClient)

    agent = StackUpdateAgent(user_id=user_id, snap_id=snap_id)
    result = agent._run()

    assert "dispatched stack update review" in result
    assert len(dispatcher.calls) == 1
    owner, repo, prompt = dispatcher.calls[0]
    assert (owner, repo) == ("kenvandine", "my-electron-app")
    assert "Electron" in prompt
    assert "Rust" in prompt

    session = isolated_session()
    tasks = session.query(CopilotTask).filter_by(kind="stack_update").all()
    assert len(tasks) == 1
    assert tasks[0].owner_repo == "kenvandine/my-electron-app"
    assert tasks[0].snap_id == snap_id
    session.close()


def test_no_snap_id_skips(isolated_session) -> None:
    agent = StackUpdateAgent(user_id=1, snap_id=None)
    assert agent._run() == "no user_id/snap_id — skipped"


def test_no_packaging_repo_skips(isolated_session, monkeypatch) -> None:
    session = isolated_session()
    user = User(github_login="kenvandine", github_id=1)
    session.add(user)
    session.flush()
    uc = UserConfig(user_id=user.id, bot_github_token="tok")
    session.add(uc)
    snap = Snap(user_id=user.id, name="bare-snap")
    session.add(snap)
    session.commit()
    user_id, snap_id = user.id, snap.id
    session.close()

    session = isolated_session()
    uc = session.query(UserConfig).filter_by(user_id=user_id).first()
    session.close()
    monkeypatch.setattr(su_module, "get_user_config", lambda uid: uc)

    agent = StackUpdateAgent(user_id=user_id, snap_id=snap_id)
    assert agent._run() == "no packaging repo configured"


def test_no_coding_backend_skips(isolated_session, monkeypatch) -> None:
    user_id, snap_id = _seed(isolated_session)
    session = isolated_session()
    uc = session.query(UserConfig).filter_by(user_id=user_id).first()
    session.close()

    monkeypatch.setattr(su_module, "get_user_config", lambda uid: uc)
    monkeypatch.setattr(su_module, "get_coding_dispatcher", lambda uc: None)
    monkeypatch.setattr(su_module, "BotGitHubClient", _FakeBotClient)

    agent = StackUpdateAgent(user_id=user_id, snap_id=snap_id)
    assert agent._run() == "no coding backend configured/available"
