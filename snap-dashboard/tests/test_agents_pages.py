"""Tests for the Agents section: registry-driven overview, sub-page tabs,
and the legacy /copilot-tasks and /stats redirects."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from snap_dashboard.agents import registry
from snap_dashboard.db.models import Base, CopilotTask, User, UserConfig
from snap_dashboard.web.routes import agents as agents_module
from snap_dashboard.web.routes import copilot_tasks as tasks_module
from snap_dashboard.web.routes import stats as stats_module


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

    for mod in (auth_module, agents_module, tasks_module, stats_module):
        if hasattr(mod, "get_session"):
            monkeypatch.setattr(mod, "get_session", _fake_get_session)
    return session_local


def _seed(session_local) -> int:
    s = session_local()
    user = User(github_login="ken", github_id=1)
    s.add(user)
    s.flush()
    s.add(UserConfig(user_id=user.id, publisher="ken"))
    s.commit()
    uid = user.id
    s.close()
    return uid


def _request(uid: int, path: str, method: str = "GET", fetch: bool = False) -> Request:
    headers = [(b"x-requested-with", b"fetch")] if fetch else []
    return Request({
        "type": "http", "method": method, "path": path, "query_string": b"",
        "headers": headers, "session": {"user_id": uid}, "app": None,
    })


def test_legacy_urls_redirect_permanently():
    r1 = asyncio.run(tasks_module.copilot_tasks_legacy())
    r2 = asyncio.run(stats_module.stats_legacy())
    assert (r1.status_code, r1.headers["location"]) == (301, "/agents/tasks")
    assert (r2.status_code, r2.headers["location"]) == (301, "/agents/stats")


def test_agents_page_renders_every_registry_agent(db):
    uid = _seed(db)
    resp = asyncio.run(agents_module.agents_page(_request(uid, "/agents")))
    assert resp.status_code == 200
    body = resp.body.decode()
    for group in registry.grouped():
        assert group["label"].replace("&", "&amp;") in body
        for a in group["agents"]:
            assert f'id="card-{a.agent_type}"' in body
            assert f"agent-palette-{a.palette}" in body
    # Section tabs are shared by all Agents sub-pages.
    for href in ("/agents/runs", "/agents/tasks", "/agents/stats"):
        assert f'href="{href}"' in body
    assert "AGENT_COLORS" not in body


def test_tasks_page_renders_under_agents(db):
    uid = _seed(db)
    s = db()
    s.add(CopilotTask(
        user_id=uid, kind="dep_update", owner_repo="ken/alpha",
        status="pr_opened", ci_status="ci_failed", prompt="x",
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    ))
    s.commit()
    s.close()
    resp = asyncio.run(tasks_module.copilot_tasks_page(_request(uid, "/agents/tasks")))
    assert resp.status_code == 200
    body = resp.body.decode()
    assert 'aria-current="page"' in body
    assert "alpha" in body
    assert "ci failed" in body.lower()


def test_scan_now_answers_json_for_fetch(db, monkeypatch):
    uid = _seed(db)
    submitted = []

    class _Runner:
        def submit(self, agent):
            submitted.append(agent)

    import snap_dashboard.agents.runner as runner_module

    monkeypatch.setattr(runner_module, "get_runner", lambda: _Runner())
    resp = asyncio.run(agents_module.scan_now(_request(uid, "/agents/scan-now", "POST", fetch=True)))
    assert resp.status_code == 200
    assert json.loads(resp.body)["ok"] is True
    assert len(submitted) == 1

    resp = asyncio.run(agents_module.scan_now(_request(uid, "/agents/scan-now", "POST")))
    assert resp.status_code == 303


def test_runs_and_stats_pages_render_with_tabs(db):
    uid = _seed(db)
    runs = asyncio.run(agents_module.agent_runs_page(_request(uid, "/agents/runs")))
    stats = asyncio.run(stats_module.stats_page(_request(uid, "/agents/stats")))
    for resp, href in ((runs, "/agents/runs"), (stats, "/agents/stats")):
        assert resp.status_code == 200
        body = resp.body.decode()
        assert f'href="{href}"' in body and 'aria-current="page"' in body
