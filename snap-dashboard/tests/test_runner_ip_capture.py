"""Regression coverage for capturing a runner's client IP on enroll/heartbeat.

Shown on the runners page (see web/templates/runners.html) purely so it's
easy to `ssh` into a specific runner box — not used for auth.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from snap_dashboard.db.models import Base, Runner, User
from snap_dashboard.runners import generate_token, hash_token
from snap_dashboard.web.routes import runner_api
from snap_dashboard.web.routes.runners import _runner_dict


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
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(runner_api, "get_session", _fake_get_session)
    return _fake_get_session


class _FakeRequest:
    """Just enough of starlette.Request for enroll()/heartbeat() to use."""

    def __init__(self, body: dict, client_host: str | None = None, forwarded_for: str | None = None):
        self._body = body
        self.client = SimpleNamespace(host=client_host) if client_host else None
        self.headers = {"x-forwarded-for": forwarded_for} if forwarded_for else {}

    async def json(self):
        return self._body


def _make_user(session) -> User:
    user = User(github_login="tester", github_id=1)
    session.add(user)
    session.flush()
    return user


@pytest.mark.anyio
async def test_enroll_captures_direct_client_ip(db):
    with db() as session:
        user = _make_user(session)
        token = "tok-123"
        session.add(Runner(
            user_id=user.id, name="pending", enrollment_token_hash=hash_token(token),
        ))
        session.flush()

    request = _FakeRequest({"token": "tok-123", "name": "laptop-1"}, client_host="192.168.1.42")
    response = await runner_api.enroll(request)
    assert response.status_code == 200

    with db() as session:
        runner = session.query(Runner).filter_by(name="laptop-1").first()
        assert runner.ip_address == "192.168.1.42"


@pytest.mark.anyio
async def test_enroll_prefers_x_forwarded_for(db):
    with db() as session:
        user = _make_user(session)
        session.add(Runner(
            user_id=user.id, name="pending", enrollment_token_hash=hash_token("tok-456"),
        ))
        session.flush()

    request = _FakeRequest(
        {"token": "tok-456", "name": "laptop-2"},
        client_host="10.0.0.1",
        forwarded_for="203.0.113.5, 10.0.0.1",
    )
    response = await runner_api.enroll(request)
    assert response.status_code == 200

    with db() as session:
        runner = session.query(Runner).filter_by(name="laptop-2").first()
        assert runner.ip_address == "203.0.113.5"


@pytest.mark.anyio
async def test_heartbeat_records_ip_address(db, monkeypatch):
    secret = generate_token()
    with db() as session:
        user = _make_user(session)
        runner = Runner(user_id=user.id, name="laptop-3", secret_hash=hash_token(secret), status="idle")
        session.add(runner)
        session.flush()
        runner_id = runner.id

    request = _FakeRequest({"status": "idle"}, client_host="192.168.1.77")
    response = await runner_api.heartbeat(
        runner_id, request, authorization=f"Bearer {secret}"
    )
    assert response.status_code == 200

    with db() as session:
        runner = session.query(Runner).get(runner_id)
        assert runner.ip_address == "192.168.1.77"


def test_runner_dict_includes_captured_ip_address():
    """Regression test: the /runners page route builds a plain dict per
    runner for the template (see web/routes/runners.py) — it must forward
    ``ip_address`` too, otherwise a captured IP silently never reaches the
    page even though it's correctly stored in the DB."""
    runner = Runner(user_id=1, name="laptop-4", ip_address="192.168.1.88")
    row = _runner_dict(runner)
    assert row["ip_address"] == "192.168.1.88"
