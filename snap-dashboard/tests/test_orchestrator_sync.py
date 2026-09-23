"""Regression test for a NameError crash in sync_test_runs().

``sync_test_runs`` referenced a module-level-looking ``auto_promote_run_ids``
set that was actually declared (and left unused) inside a *different*
function (``trigger_workflow``). Any time a test run's status flipped to
"passed", ``sync_test_runs`` would raise ``NameError`` instead of queuing
auto-promotion — meaning the auto-promote feature never actually worked.

This test exercises ``sync_test_runs`` end-to-end against an isolated
in-memory SQLite database (via monkeypatching ``get_session`` inside the
orchestrator module) so it never touches the real dashboard database.
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
    """Point orchestrator.get_session at a throwaway in-memory database."""
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


def test_sync_test_runs_promotes_newly_passed_run_without_crashing(monkeypatch, isolated_session):
    session_local = isolated_session

    # Seed one in-flight TestRun that is about to be reported as "passed".
    with session_local() as session:
        run = TestRun(
            snap_name="gedit",
            architecture="amd64",
            from_channel="candidate",
            version="46.0",
            status="running",
            triggered_by="auto",
        )
        session.add(run)
        session.commit()
        run_id = run.id

    fake_pr = {
        "number": 42,
        "html_url": "https://github.com/kenvandine/gedit-tests/pull/42",
        "body": "snap: gedit\nversion: 46.0\nstatus: passed\n",
    }

    def _fake_get_test_prs(testing_repo, token):
        return [fake_pr]

    def _fake_parse_pr_metadata(body):
        return {"snap": "gedit", "version": "46.0", "status": "passed"}

    monkeypatch.setattr("snap_dashboard.github.pr_viewer.get_test_prs", _fake_get_test_prs)
    monkeypatch.setattr("snap_dashboard.github.pr_viewer.parse_pr_metadata", _fake_parse_pr_metadata)

    promoted_ids: list[int] = []
    monkeypatch.setattr(orchestrator, "_maybe_submit_auto_promoter", promoted_ids.append)

    # This used to raise NameError: name 'auto_promote_run_ids' is not defined.
    orchestrator.sync_test_runs(testing_repo="kenvandine/gedit-tests", github_token="tok")

    assert promoted_ids == [run_id]

    with session_local() as session:
        updated = session.query(TestRun).get(run_id)
        assert updated.status == "passed"
