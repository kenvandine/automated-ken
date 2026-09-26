"""Regression test for the Coding Tasks "Retry" action.

Before this fix, a failed CopilotTask dispatch (dispatch_failed/failed) was
a dead end: the "Refresh" button only shows for non-terminal tasks, and
error_msg was never populated for dispatch failures, so a failed row had no
error text and no way to act on it. This exercises the new
POST /copilot-tasks/{id}/retry route end to end: it should insert a brand
new "dispatching" CopilotTask row immediately (preserving the original for
history) and hand the actual (potentially slow) dispatch off to a
background agent (``RetryCopilotTaskAgent``) instead of blocking the
request — see that module's docstring for why: doing the blocking
``start_task()`` call inline inside this ``async def`` route previously
froze the entire web UI for every user for as long as the dispatch took.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from snap_dashboard.agents import copilot_retry as retry_module
from snap_dashboard.db.models import Base, CopilotTask, User
from snap_dashboard.web.routes import copilot_tasks as routes_module


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

    monkeypatch.setattr(routes_module, "get_session", _fake_get_session)
    monkeypatch.setattr(retry_module, "get_session", _fake_get_session)
    return session_local


class _FakeRequest:
    pass


class _FakeRunner:
    """Runs a submitted agent's ``_run()`` synchronously, in-process.

    The real ``AgentRunner`` submits to a background thread pool — fine in
    production, but tests want the retry's effects to be visible
    immediately without needing to wire up threads/polling. This bypasses
    ``BaseAgent.run()``'s AgentRun bookkeeping/log-capture wrapper (covered
    separately by ``test_agent_run_logs.py``) and just calls ``_run()``.
    """

    def __init__(self) -> None:
        self.submitted = []

    def submit(self, agent) -> None:
        self.submitted.append(agent)
        agent._run()


class _FakeDispatcher:
    def __init__(self, task=None, last_error=None):
        self.calls = []
        self._task = task
        self.last_error = last_error

    def start_task(self, owner, repo, prompt, base_ref="main", create_pull_request=True, model=None):
        self.calls.append((owner, repo, prompt, base_ref))
        return self._task


def _seed_failed_task(session_local, **overrides) -> tuple[int, int]:
    session = session_local()
    user = User(github_login="kenvandine", github_id=1)
    session.add(user)
    session.flush()
    kwargs = dict(
        user_id=user.id,
        snap_id=None,
        kind="fleet_normalize",
        owner_repo="kenvandine/my-snap",
        prompt="do the thing",
        base_ref="main",
        status="dispatch_failed",
        error_msg="No Copilot license on this GitHub account",
    )
    kwargs.update(overrides)
    task = CopilotTask(**kwargs)
    session.add(task)
    session.commit()
    user_id, task_id = user.id, task.id
    session.close()
    return user_id, task_id


def _patch_runner(monkeypatch) -> _FakeRunner:
    runner = _FakeRunner()
    monkeypatch.setattr("snap_dashboard.agents.runner.get_runner", lambda: runner)
    return runner


@pytest.mark.anyio
async def test_retry_creates_new_task_and_preserves_original(isolated_session, monkeypatch):
    user_id, task_id = _seed_failed_task(isolated_session)

    monkeypatch.setattr(routes_module, "get_current_user", lambda request: {"id": user_id})
    monkeypatch.setattr(retry_module, "get_user_config", lambda uid: SimpleNamespace())
    dispatcher = _FakeDispatcher(task={"id": "task-99"})
    monkeypatch.setattr(retry_module, "get_coding_dispatcher", lambda uc: dispatcher)
    runner = _patch_runner(monkeypatch)

    resp = await routes_module.retry_copilot_task(task_id, _FakeRequest())
    assert resp.status_code == 303
    assert len(runner.submitted) == 1

    assert dispatcher.calls == [("kenvandine", "my-snap", "do the thing", "main")]

    session = isolated_session()
    tasks = session.query(CopilotTask).filter_by(kind="fleet_normalize").order_by(CopilotTask.id).all()
    assert len(tasks) == 2
    assert tasks[0].id == task_id
    assert tasks[0].status == "dispatch_failed"  # original preserved untouched
    assert tasks[1].status == "queued"
    assert tasks[1].owner_repo == "kenvandine/my-snap"
    assert tasks[1].prompt == "do the thing"
    assert tasks[1].base_ref == "main"
    session.close()


@pytest.mark.anyio
async def test_retry_populates_error_msg_on_repeat_failure(isolated_session, monkeypatch):
    user_id, task_id = _seed_failed_task(isolated_session)

    monkeypatch.setattr(routes_module, "get_current_user", lambda request: {"id": user_id})
    monkeypatch.setattr(retry_module, "get_user_config", lambda uid: SimpleNamespace())
    dispatcher = _FakeDispatcher(task=None, last_error="still no Copilot license")
    monkeypatch.setattr(retry_module, "get_coding_dispatcher", lambda uc: dispatcher)
    _patch_runner(monkeypatch)

    await routes_module.retry_copilot_task(task_id, _FakeRequest())

    session = isolated_session()
    tasks = session.query(CopilotTask).filter_by(kind="fleet_normalize").order_by(CopilotTask.id).all()
    assert len(tasks) == 2
    assert tasks[1].status == "dispatch_failed"
    assert tasks[1].error_msg == "still no Copilot license"
    session.close()


@pytest.mark.anyio
async def test_retry_no_op_when_prompt_missing(isolated_session, monkeypatch):
    user_id, task_id = _seed_failed_task(isolated_session, prompt=None)

    monkeypatch.setattr(routes_module, "get_current_user", lambda request: {"id": user_id})
    dispatcher = _FakeDispatcher(task={"id": "task-1"})
    monkeypatch.setattr(retry_module, "get_coding_dispatcher", lambda uc: dispatcher)
    runner = _patch_runner(monkeypatch)

    await routes_module.retry_copilot_task(task_id, _FakeRequest())

    assert dispatcher.calls == []
    assert runner.submitted == []
    session = isolated_session()
    tasks = session.query(CopilotTask).filter_by(kind="fleet_normalize").all()
    assert len(tasks) == 1  # nothing new inserted
    session.close()
