"""Regression tests for PRMonitorAgent polling a delegated version-bump task.

``VersionBumperAgent`` can leave a ``VersionBumpPR`` in the "dispatched"
state when it hands the bump off to an async coding backend (GitHub
Copilot cloud agent) that hasn't produced a PR yet. ``PRMonitorAgent`` is
responsible for polling that task and advancing the bump to "open" (with
the real PR number/branch) once it completes, or to "closed" if the task
ends without ever producing a PR.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from snap_dashboard.agents import pr_monitor as prm
from snap_dashboard.db.models import Base, Snap, VersionBumpPR


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


def _seed_dispatched(session_local) -> int:
    with session_local() as session:
        snap = Snap(name="godot-4", packaging_repo="https://github.com/kenvandine/godot-snap")
        session.add(snap)
        session.flush()
        bump = VersionBumpPR(
            snap_id=snap.id,
            packaging_repo="https://github.com/kenvandine/godot-snap",
            old_version="4.2",
            new_version="4.3",
            status="dispatched",
            external_task_id="task-123",
        )
        session.add(bump)
        session.commit()
        return bump.id


def test_check_dispatched_resolves_to_open_on_completed_task(isolated_session, monkeypatch):
    bump_id = _seed_dispatched(isolated_session)

    class _FakeCopilotClient:
        def __init__(self, token):
            pass

        def get_task(self, owner, repo, task_id):
            return {"state": "completed", "pull_request_url": "https://github.com/kenvandine/godot-snap/pull/7"}

    monkeypatch.setattr(prm, "CopilotAgentClient", _FakeCopilotClient)

    class _FakeResp:
        status_code = 200

        def json(self):
            return {"head": {"ref": "copilot/bump-godot"}}

    monkeypatch.setattr(prm.httpx.Client, "__enter__", lambda self: self)
    monkeypatch.setattr(prm.httpx.Client, "__exit__", lambda self, *a: None)
    monkeypatch.setattr(prm.httpx.Client, "get", lambda self, *a, **k: _FakeResp())

    agent = prm.PRMonitorAgent()
    changed = agent._check_dispatched(
        {"id": bump_id, "packaging_repo": "https://github.com/kenvandine/godot-snap", "external_task_id": "task-123"},
        "bot-token",
    )

    assert changed is True
    with isolated_session() as session:
        bump = session.query(VersionBumpPR).get(bump_id)
        assert bump.status == "open"
        assert bump.bot_pr_number == 7
        assert bump.bot_pr_url == "https://github.com/kenvandine/godot-snap/pull/7"
        assert bump.branch_name == "copilot/bump-godot"


def test_check_dispatched_closes_on_failed_task(isolated_session, monkeypatch):
    bump_id = _seed_dispatched(isolated_session)

    class _FakeCopilotClient:
        def __init__(self, token):
            pass

        def get_task(self, owner, repo, task_id):
            return {"state": "failed", "error": "could not apply patch"}

    monkeypatch.setattr(prm, "CopilotAgentClient", _FakeCopilotClient)

    agent = prm.PRMonitorAgent()
    changed = agent._check_dispatched(
        {"id": bump_id, "packaging_repo": "https://github.com/kenvandine/godot-snap", "external_task_id": "task-123"},
        "bot-token",
    )

    assert changed is True
    with isolated_session() as session:
        bump = session.query(VersionBumpPR).get(bump_id)
        assert bump.status == "closed"


def test_check_dispatched_leaves_in_progress_task_alone(isolated_session, monkeypatch):
    bump_id = _seed_dispatched(isolated_session)

    class _FakeCopilotClient:
        def __init__(self, token):
            pass

        def get_task(self, owner, repo, task_id):
            return {"state": "in_progress"}

    monkeypatch.setattr(prm, "CopilotAgentClient", _FakeCopilotClient)

    agent = prm.PRMonitorAgent()
    changed = agent._check_dispatched(
        {"id": bump_id, "packaging_repo": "https://github.com/kenvandine/godot-snap", "external_task_id": "task-123"},
        "bot-token",
    )

    assert changed is False
    with isolated_session() as session:
        bump = session.query(VersionBumpPR).get(bump_id)
        assert bump.status == "dispatched"
