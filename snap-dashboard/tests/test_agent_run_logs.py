"""Tests for the agent run log capture + viewing feature.

Covers:
- ``BaseAgent.run()`` captures the agent's own log output (and that of
  anything it calls) into ``AgentRun.log_output``, on both success and
  error, via the per-thread capture handler in agents/base.py.
- ``/agents/runs/{id}/log`` returns that captured text.
- ``/agents/runs`` lists/filters runs and correctly reports ``has_log``.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from snap_dashboard.agents.base import BaseAgent
from snap_dashboard.db.models import AgentRun, Base, User
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
    monkeypatch.setattr("snap_dashboard.agents.base.get_session", _fake_get_session)
    return session_local


class _OkAgent(BaseAgent):
    agent_type = "release_scanner"

    def _run(self) -> str:
        logging.getLogger("snap_dashboard.agents.release_scanner").info("checked evince")
        return "1 release found"


class _ErrAgent(BaseAgent):
    agent_type = "version_bumper"

    def _run(self) -> str:
        logging.getLogger("snap_dashboard.agents.version_bumper").info("about to fail")
        raise RuntimeError("kaboom")


class _FakeRequest:
    def __init__(self, user_id: int) -> None:
        self.session = {"user_id": user_id}

    def query_params_get(self, *_a, **_k):
        return None


def test_successful_run_captures_log(isolated_session):
    session = isolated_session()
    user = User(github_login="ken", github_id=1)
    session.add(user)
    session.commit()
    user_id = user.id
    session.close()

    _OkAgent(user_id=user_id, snap_name="evince").run()

    session2 = isolated_session()
    run = session2.query(AgentRun).filter_by(user_id=user_id).one()
    assert run.status == "done"
    assert "checked evince" in run.log_output
    session2.close()


def test_error_run_captures_log_and_traceback(isolated_session):
    session = isolated_session()
    user = User(github_login="ken", github_id=1)
    session.add(user)
    session.commit()
    user_id = user.id
    session.close()

    _ErrAgent(user_id=user_id).run()

    session2 = isolated_session()
    run = session2.query(AgentRun).filter_by(user_id=user_id).one()
    assert run.status == "error"
    assert "about to fail" in run.log_output
    assert "RuntimeError: kaboom" in run.log_output
    session2.close()


@pytest.mark.anyio
async def test_run_log_route_returns_captured_text(monkeypatch, isolated_session):
    session = isolated_session()
    user = User(github_login="ken", github_id=1)
    session.add(user)
    session.commit()
    user_id = user.id
    session.close()

    _OkAgent(user_id=user_id, snap_name="evince").run()

    session2 = isolated_session()
    run = session2.query(AgentRun).filter_by(user_id=user_id).one()
    run_id = run.id
    session2.close()

    monkeypatch.setattr(agents_module, "get_current_user", lambda request: {"id": user_id})

    response = await agents_module.agent_run_log(run_id, _FakeRequest(user_id))
    body = response.body.decode()
    assert "checked evince" in body


@pytest.mark.anyio
async def test_run_log_route_404_for_missing_run(monkeypatch, isolated_session):
    monkeypatch.setattr(agents_module, "get_current_user", lambda request: {"id": 1})
    response = await agents_module.agent_run_log(999, _FakeRequest(1))
    assert response.status_code == 404


@pytest.mark.anyio
async def test_runs_page_filters_and_reports_has_log(monkeypatch, isolated_session):
    session = isolated_session()
    user = User(github_login="ken", github_id=1)
    session.add(user)
    session.commit()
    user_id = user.id
    session.close()

    _OkAgent(user_id=user_id, snap_name="evince").run()
    _ErrAgent(user_id=user_id).run()

    monkeypatch.setattr(agents_module, "get_current_user", lambda request: {"id": user_id})

    captured = {}

    def _fake_template_response(request, name, context):
        captured["context"] = context
        return "rendered"

    monkeypatch.setattr(agents_module.templates, "TemplateResponse", _fake_template_response)

    await agents_module.agent_runs_page(_FakeRequest(user_id), agent_type="", status="")
    ctx = captured["context"]
    assert ctx["total"] == 2
    by_type = {r["agent_type"]: r for r in ctx["runs"]}
    assert by_type["release_scanner"]["has_log"] is True
    assert by_type["version_bumper"]["has_log"] is True

    captured.clear()
    await agents_module.agent_runs_page(_FakeRequest(user_id), agent_type="release_scanner", status="")
    ctx = captured["context"]
    assert ctx["total"] == 1
    assert ctx["runs"][0]["agent_type"] == "release_scanner"
