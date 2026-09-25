"""Regression test for the Agents page pipeline stage counts.

``/api/agent-status``'s pipeline counts were computed solely from
``VersionBumpPR.status``. Since the run_id-based testing flow (see
``TestRun.pr_number`` and ``testing/orchestrator.py``) lets a YARF test run
exist without ever having a version-bump PR (manually-triggered runs,
manually-added snaps), the pipeline showed a permanently-stuck 0 for
"YARF Tests"/"AI Review"/"Approved"/"Merged" whenever the real, live work was
happening through standalone test runs rather than bot-opened PRs -- even
though "New Releases" (sourced from a different table) displayed correctly.
"""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from snap_dashboard.db.models import Base, TestRun, User
from snap_dashboard.web.routes import agents as agents_module


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

    monkeypatch.setattr(agents_module, "get_session", _fake_get_session)
    return session_local


class _FakeTracker:
    def get_active(self, _user_id=None):
        return {}

    def get_log_since(self, _seq, _user_id=None):
        return []

    def latest_seq(self):
        return 0


class _FakeRunner:
    def get_schedules(self):
        return []


class _FakeRequest:
    def __init__(self, user_id: int) -> None:
        self.session = {"user_id": user_id}


@pytest.mark.anyio
async def test_pipeline_counts_standalone_test_runs(monkeypatch, isolated_session):
    session = isolated_session()
    user = User(github_login="ken", github_id=1)
    session.add(user)
    session.commit()
    user_id = user.id

    # A standalone run (no version-bump PR) at each pipeline stage.
    session.add_all([
        TestRun(user_id=user_id, snap_name="evince", from_channel="candidate", status="running"),
        TestRun(user_id=user_id, snap_name="gedit", from_channel="candidate", status="reviewing"),
        TestRun(
            user_id=user_id, snap_name="totem", from_channel="candidate",
            status="passed", review_decision="approve",
        ),
        TestRun(
            user_id=user_id, snap_name="nautilus", from_channel="candidate",
            status="passed", review_decision=None,
        ),
        TestRun(user_id=user_id, snap_name="files", from_channel="candidate", status="promoted"),
    ])
    session.commit()
    session.close()

    monkeypatch.setattr(
        agents_module,
        "get_current_user",
        lambda request: {"id": user_id, "github_login": "ken"},
    )
    monkeypatch.setattr(
        agents_module, "get_user_config", lambda uid: type("UC", (), {"lemonade_server_url": "", "lemonade_model": "", "lemonade_backend": "embedded"})()
    )
    monkeypatch.setattr("snap_dashboard.agents.runner.get_runner", lambda: _FakeRunner())
    monkeypatch.setattr("snap_dashboard.agents.runner.get_tracker", lambda: _FakeTracker())

    response = agents_module.agent_status(_FakeRequest(user_id))
    body = response.body.decode()
    data = json.loads(body)
    pipeline = data["pipeline"]

    assert pipeline["yarf_running"] == 1
    # "reviewing" plus the "passed"-but-undecided run both need attention.
    assert pipeline["under_review"] == 2
    assert pipeline["approved"] == 1
    assert pipeline["merged"] == 1
