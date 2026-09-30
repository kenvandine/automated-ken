"""Tests for the Overview (/) read model and page."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from snap_dashboard.db.models import (
    AgentRun,
    Base,
    CopilotTask,
    Runner,
    Snap,
    TestRun,
    User,
    UserConfig,
    VersionBumpPR,
)
from snap_dashboard.web.fleet import build_overview
from snap_dashboard.web.routes import dashboard as dashboard_module


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    @contextmanager
    def _fake_get_session():
        session = session_local()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    import snap_dashboard.auth as auth_module

    for mod in (auth_module, dashboard_module):
        monkeypatch.setattr(mod, "get_session", _fake_get_session)
    monkeypatch.setattr(dashboard_module, "get_user_config", auth_module.get_user_config)
    return session_local


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _seed(session_local) -> int:
    s = session_local()
    user = User(github_login="ken", github_id=1)
    s.add(user)
    s.flush()
    s.add(UserConfig(user_id=user.id, publisher="ken", github_token="gh"))
    a = Snap(name="alpha", user_id=user.id, packaging_repo="https://github.com/k/alpha")
    b = Snap(name="beta", user_id=user.id, packaging_repo="https://github.com/k/beta")
    s.add_all([a, b])
    s.flush()
    now = _now()
    s.add_all(
        [
            TestRun(user_id=user.id, snap_name="alpha", version="2.0", architecture="amd64",
                    from_channel="candidate", status="failed", started_at=now),
            TestRun(user_id=user.id, snap_name="beta", version="3.0", architecture="amd64",
                    from_channel="candidate", status="running", started_at=now),
            TestRun(user_id=user.id, snap_name="beta", version="2.9", architecture="arm64",
                    from_channel="candidate", status="passed", promoted=True,
                    promoted_at=now - timedelta(days=1), started_at=now - timedelta(days=2)),
            VersionBumpPR(snap_id=a.id, user_id=user.id, status="ci_failed", new_version="2.1",
                          bot_pr_url="https://github.com/k/alpha/pull/9"),
            VersionBumpPR(snap_id=b.id, user_id=user.id, status="ci_pending", new_version="3.1"),
            CopilotTask(user_id=user.id, kind="ci_fix", owner_repo="k/alpha", status="waiting_for_user"),
            CopilotTask(user_id=user.id, kind="dep_update", owner_repo="k/beta", status="in_progress"),
            AgentRun(user_id=user.id, agent_type="collector", status="running"),
            Runner(user_id=user.id, name="old-box", arch="arm64", status="idle",
                   last_heartbeat_at=now - timedelta(hours=1)),
            Runner(user_id=user.id, name="fresh-box", arch="amd64", status="idle",
                   last_heartbeat_at=now),
            Runner(user_id=user.id, name="gone", status="idle", revoked_at=now),
        ]
    )
    s.commit()
    uid = user.id
    s.close()
    return uid


def test_build_overview_groups_work(db):
    uid = _seed(db)
    s = db()
    ov = build_overview(s, uid)
    s.close()

    kinds = sorted(n["kind"] for n in ov["needs"])
    assert kinds == ["bump", "runner", "task", "test"]
    bump = next(n for n in ov["needs"] if n["kind"] == "bump")
    assert bump["tone"] == "negative"
    assert bump["external"] == "https://github.com/k/alpha/pull/9"
    runner = next(n for n in ov["needs"] if n["kind"] == "runner")
    assert runner["title"] == "old-box"

    assert [r["snap"] for r in ov["active_runs"]] == ["beta"]
    assert [b["status"] for b in ov["in_flight_bumps"]] == ["ci_pending"]
    assert [t["status"] for t in ov["active_tasks"]] == ["in_progress"]
    assert len(ov["running_agents"]) == 1
    assert ov["shipped"] == [
        {"snap": "beta", "version": "2.9", "arches": ["arm64"], "when": ov["shipped"][0]["when"]}
    ]
    st = ov["stats"]
    assert st["snaps"] == 2
    assert st["needs"] == 4
    assert st["in_flight"] == 4
    assert (st["runners_online"], st["runners_total"]) == (1, 2)


def test_build_overview_empty_is_quiet(db):
    s = db()
    user = User(github_login="q", github_id=2)
    s.add(user)
    s.commit()
    ov = build_overview(s, user.id)
    s.close()
    assert ov["needs"] == [] and ov["shipped"] == [] and ov["stats"]["in_flight"] == 0


def test_overview_page_renders(db):
    uid = _seed(db)
    req = Request({
        "type": "http", "method": "GET", "path": "/", "query_string": b"",
        "headers": [], "session": {"user_id": uid}, "app": None,
    })
    resp = asyncio.run(dashboard_module.dashboard_index(req))
    assert resp.status_code == 200
    body = resp.body.decode()
    assert "Needs you" in body
    assert "old-box" in body
    assert "alpha → 2.1" in body
    assert 'href="/snaps"' in body
