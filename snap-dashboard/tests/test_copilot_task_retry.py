"""Regression test for the Coding Tasks "Retry" action.

Before this fix, a failed CopilotTask dispatch (dispatch_failed/failed) was
a dead end: the "Refresh" button only shows for non-terminal tasks, and
error_msg was never populated for dispatch failures, so a failed row had no
error text and no way to act on it. This exercises the new
POST /copilot-tasks/{id}/retry route end to end: it should insert a brand
new CopilotTask row (preserving the original for history) using the same
prompt/base_ref/kind, and populate error_msg on failure via the
dispatcher's ``last_error``.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

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
    return session_local


class _FakeRequest:
    pass


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


@pytest.mark.anyio
async def test_retry_creates_new_task_and_preserves_original(isolated_session, monkeypatch):
    user_id, task_id = _seed_failed_task(isolated_session)

    monkeypatch.setattr(routes_module, "get_current_user", lambda request: {"id": user_id})
    monkeypatch.setattr(routes_module, "get_user_config", lambda uid: SimpleNamespace())
    dispatcher = _FakeDispatcher(task={"id": "task-99"})
    monkeypatch.setattr(routes_module, "get_coding_dispatcher", lambda uc: dispatcher)

    resp = await routes_module.retry_copilot_task(task_id, _FakeRequest())
    assert resp.status_code == 303

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
    monkeypatch.setattr(routes_module, "get_user_config", lambda uid: SimpleNamespace())
    dispatcher = _FakeDispatcher(task=None, last_error="still no Copilot license")
    monkeypatch.setattr(routes_module, "get_coding_dispatcher", lambda uc: dispatcher)

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
    monkeypatch.setattr(routes_module, "get_coding_dispatcher", lambda uc: dispatcher)

    await routes_module.retry_copilot_task(task_id, _FakeRequest())

    assert dispatcher.calls == []
    session = isolated_session()
    tasks = session.query(CopilotTask).filter_by(kind="fleet_normalize").all()
    assert len(tasks) == 1  # nothing new inserted
    session.close()
