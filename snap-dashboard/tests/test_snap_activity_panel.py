"""Tests for the per-snap "Agent Activity" panel added to the snap detail
page — ``GET /api/snap/{name}/activity`` and the underlying
``ActivityTracker.get_active_for_snap`` helper.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from snap_dashboard.agents.runner import ActivityTracker
from snap_dashboard.db.models import AgentRun, Base, CopilotTask, Snap, TestRun, User
from snap_dashboard.web.routes import agents as agents_module
from snap_dashboard.web.routes import snaps as snaps_module


# ---------------------------------------------------------------------------
# ActivityTracker.get_active_for_snap
# ---------------------------------------------------------------------------

def test_get_active_for_snap_filters_by_snap_and_is_not_collapsed_by_type():
    tracker = ActivityTracker()
    tracker.set_active("k1", "rebuild_one_snap", "Rebuilding neofetch-desktop…", snap_name="neofetch-desktop", user_id=1)
    tracker.set_active("k2", "rebuild_one_snap", "Rebuilding other-snap…", snap_name="other-snap", user_id=1)
    tracker.set_active("k3", "custom_prompt", "Dispatching custom task for neofetch-desktop…", snap_name="neofetch-desktop", user_id=1)

    active = tracker.get_active_for_snap("neofetch-desktop", user_id=1)
    tasks = sorted(a["task"] for a in active)
    assert tasks == [
        "Dispatching custom task for neofetch-desktop…",
        "Rebuilding neofetch-desktop…",
    ]

    # get_active() collapses by agent_type, so it would only show one of the
    # two rebuild_one_snap entries — get_active_for_snap must not do that.
    assert len(tracker.get_active_for_snap("neofetch-desktop", user_id=1)) == 2


def test_get_active_for_snap_respects_user_visibility():
    tracker = ActivityTracker()
    tracker.set_active("k1", "rebuild_one_snap", "Rebuilding x…", snap_name="x", user_id=1)
    assert tracker.get_active_for_snap("x", user_id=2) == []
    assert tracker.get_active_for_snap("x", user_id=1) != []
    # Global (no-user) entries are visible to everyone.
    tracker.set_active("k2", "build_failure_watcher", "Checking x…", snap_name="x", user_id=None)
    assert len(tracker.get_active_for_snap("x", user_id=2)) == 1


def test_get_active_for_snap_case_insensitive():
    tracker = ActivityTracker()
    tracker.set_active("k1", "rebuild_one_snap", "Rebuilding X…", snap_name="X", user_id=1)
    assert len(tracker.get_active_for_snap("x", user_id=1)) == 1


# ---------------------------------------------------------------------------
# GET /api/snap/{name}/activity
# ---------------------------------------------------------------------------

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

    monkeypatch.setattr(snaps_module, "get_session", _fake_get_session)
    monkeypatch.setattr(agents_module, "get_session", _fake_get_session)
    return session_local


class _FakeRequest:
    def __init__(self, user_id: int) -> None:
        self.session = {"user_id": user_id}


@pytest.fixture
def isolated_tracker(monkeypatch):
    """A fresh ActivityTracker, isolated from the process-wide singleton.

    The real ``get_tracker()`` singleton accumulates "active" entries left
    behind by *other* test modules that call agent internals directly
    without going through ``BaseAgent.run()``'s ``clear_active()`` teardown
    (e.g. test_build_failure_watcher.py) — polluting these tests when run
    as part of the full suite depending on execution order. Patching
    ``get_tracker`` at its source module keeps this test file's assertions
    deterministic regardless of what ran before it.
    """
    tracker = ActivityTracker()
    monkeypatch.setattr("snap_dashboard.agents.runner.get_tracker", lambda: tracker)
    return tracker


def _seed_snap(session_local, snap_name="neofetch-desktop"):
    session = session_local()
    user = User(github_login="kenvandine", github_id=1)
    session.add(user)
    session.flush()
    snap = Snap(user_id=user.id, name=snap_name, packaging_repo=f"kenvandine/{snap_name}")
    session.add(snap)
    session.commit()
    user_id, snap_id = user.id, snap.id
    session.close()
    return user_id, snap_id


def test_activity_endpoint_reports_queued_task_and_pending_test(monkeypatch, isolated_session, isolated_tracker):
    user_id, snap_id = _seed_snap(isolated_session)

    session = isolated_session()
    session.add(CopilotTask(user_id=user_id, snap_id=snap_id, kind="custom_prompt", owner_repo="kenvandine/neofetch-desktop", status="in_progress"))
    session.add(TestRun(user_id=user_id, snap_name="neofetch-desktop", architecture="amd64", from_channel="candidate", status="running", started_at=datetime.now(timezone.utc)))
    session.commit()
    session.close()

    monkeypatch.setattr(snaps_module, "get_current_user", lambda request: {"id": user_id})

    response = snaps_module.snap_activity("neofetch-desktop", _FakeRequest(user_id))
    import json
    data = json.loads(response.body)

    assert data["busy"] is True
    assert len(data["queued_tasks"]) == 1
    assert data["queued_tasks"][0]["kind"] == "custom_prompt"
    assert len(data["pending_tests"]) == 1
    assert data["pending_tests"][0]["architecture"] == "amd64"
    assert data["active"] == []
    assert data["recent"] == []


def test_activity_endpoint_shows_recent_finished_runs(monkeypatch, isolated_session, isolated_tracker):
    user_id, snap_id = _seed_snap(isolated_session)

    session = isolated_session()
    run = AgentRun(
        user_id=user_id,
        agent_type="rebuild_one_snap",
        snap_name="neofetch-desktop",
        status="done",
        result_summary="neofetch-desktop: rebuild triggered",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )
    session.add(run)
    session.commit()
    session.close()

    monkeypatch.setattr(snaps_module, "get_current_user", lambda request: {"id": user_id})
    response = snaps_module.snap_activity("neofetch-desktop", _FakeRequest(user_id))
    import json
    data = json.loads(response.body)

    assert data["busy"] is False
    assert len(data["recent"]) == 1
    assert data["recent"][0]["agent_type"] == "rebuild_one_snap"
    assert data["recent"][0]["status"] == "done"


def test_activity_endpoint_requires_auth(monkeypatch, isolated_session):
    monkeypatch.setattr(snaps_module, "get_current_user", lambda request: None)
    response = snaps_module.snap_activity("neofetch-desktop", _FakeRequest(1))
    assert response.status_code == 401


def test_activity_endpoint_unknown_snap_returns_empty(monkeypatch, isolated_session, isolated_tracker):
    user = User(github_login="kenvandine", github_id=1)
    session = isolated_session()
    session.add(user)
    session.commit()
    user_id = user.id
    session.close()

    monkeypatch.setattr(snaps_module, "get_current_user", lambda request: {"id": user_id})
    response = snaps_module.snap_activity("does-not-exist", _FakeRequest(user_id))
    import json
    data = json.loads(response.body)
    assert data == {"active": [], "queued_tasks": [], "pending_tests": [], "recent": [], "busy": False}


# ---------------------------------------------------------------------------
# /agents/runs?snap_name=... filter
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_agent_runs_page_filters_by_snap_name(monkeypatch, isolated_session):
    user_id, _snap_id = _seed_snap(isolated_session)

    session = isolated_session()
    session.add(AgentRun(user_id=user_id, agent_type="rebuild_one_snap", snap_name="neofetch-desktop", status="done", started_at=datetime.now(timezone.utc)))
    session.add(AgentRun(user_id=user_id, agent_type="rebuild_one_snap", snap_name="other-snap", status="done", started_at=datetime.now(timezone.utc)))
    session.commit()
    session.close()

    monkeypatch.setattr(agents_module, "get_current_user", lambda request: {"id": user_id})

    captured = {}

    def _fake_template_response(request, name, context):
        captured["context"] = context
        return "rendered"

    monkeypatch.setattr(agents_module.templates, "TemplateResponse", _fake_template_response)

    await agents_module.agent_runs_page(_FakeRequest(user_id), snap_name="neofetch-desktop")
    ctx = captured["context"]
    assert ctx["total"] == 1
    assert ctx["runs"][0]["snap_name"] == "neofetch-desktop"
