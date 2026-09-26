"""Regression test for the "Re-run" dead end on trigger_remote_run's
``skip_if_exists`` duplicate guard.

Ad-hoc "Run test" triggers (snap detail page, Testing page) pass
``skip_if_exists=True`` so re-clicking the same revision doesn't shadow an
already-finished run with a fresh "pending" row (see trigger_remote_run's
docstring). When that guard fires, the error message told the user to
"use Re-run if you want to test it again" — but no such control existed
anywhere reachable from those pages, a dead end. The fix threads a
``force`` flag through to bypass ``skip_if_exists`` on demand; this test
exercises the underlying orchestrator behavior that flag relies on.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from snap_dashboard.db.models import Base, TestRun
from snap_dashboard.testing import orchestrator


@pytest.fixture
def isolated_session(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)

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

    monkeypatch.setattr(orchestrator, "get_session", _fake_get_session)
    return session_local


def test_second_trigger_is_blocked_with_a_reference_to_the_existing_run(isolated_session):
    ok1, err1, run_id1 = orchestrator.trigger_remote_run(
        "evince", "candidate", "45.0", 17, architecture="amd64", user_id=1, skip_if_exists=True,
    )
    assert ok1
    assert run_id1 is not None

    ok2, err2, run_id2 = orchestrator.trigger_remote_run(
        "evince", "candidate", "45.0", 17, architecture="amd64", user_id=1, skip_if_exists=True,
    )
    assert not ok2
    assert "use Re-run" in err2
    assert str(run_id1) in err2
    # No duplicate row was created — the returned id is the existing run's.
    assert run_id2 == run_id1


def test_force_bypasses_the_duplicate_guard_and_queues_a_fresh_run(isolated_session):
    session_local = isolated_session

    ok1, _err1, run_id1 = orchestrator.trigger_remote_run(
        "evince", "candidate", "45.0", 17, architecture="amd64", user_id=1, skip_if_exists=True,
    )
    assert ok1

    ok2, err2, run_id2 = orchestrator.trigger_remote_run(
        "evince", "candidate", "45.0", 17, architecture="amd64", user_id=1, skip_if_exists=False,
    )
    assert ok2, err2
    assert run_id2 != run_id1

    with session_local() as session:
        runs = session.query(TestRun).filter_by(snap_name="evince").all()
        assert len(runs) == 2
