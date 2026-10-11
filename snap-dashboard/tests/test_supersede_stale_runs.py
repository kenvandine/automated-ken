"""Regression test: queued/passed-unpromoted runs for an older version of
the same snap+channel+architecture must be superseded once a newer
version/revision shows up.

Reported bug: the "To release" page kept piling up one "Ready to promote"
card per candidate version a snap had ever reached (e.g. pdfcraft 0.6.0,
0.5.0, 0.4.0 all shown as ready at once) instead of only the current one,
and a newer version's test could be queued behind an already-stale older
version still sitting in the runner queue.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from snap_dashboard.db.models import Base, TestRun
from snap_dashboard.testing.orchestrator import supersede_stale_runs


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_local()
    yield session
    session.close()


def _run(session, **kwargs):
    defaults = {
        "snap_name": "pdfcraft",
        "architecture": "amd64",
        "from_channel": "candidate",
        "status": "passed",
        "user_id": 1,
    }
    defaults.update(kwargs)
    run = TestRun(**defaults)
    session.add(run)
    session.flush()
    return run


def test_older_passed_unpromoted_versions_are_superseded(db_session):
    session = db_session
    old1 = _run(session, version="0.4.0", revision=10)
    old2 = _run(session, version="0.5.0", revision=11)
    newest = _run(session, version="0.6.0", revision=12)

    changed = supersede_stale_runs(session, user_id=1)

    assert changed == 2
    assert old1.status == "superseded"
    assert old2.status == "superseded"
    assert newest.status == "passed"  # the newest run is left alone


def test_queued_pending_run_is_superseded_by_a_newer_queued_version(db_session):
    session = db_session
    stale = _run(session, version="0.5.0", revision=11, status="pending")
    _run(session, version="0.6.0", revision=12, status="pending")

    changed = supersede_stale_runs(session, user_id=1)

    assert changed == 1
    assert stale.status == "superseded"


def test_in_flight_run_only_gets_cancel_requested_not_a_status_overwrite(db_session):
    session = db_session
    in_flight = _run(session, version="0.5.0", revision=11, status="running")
    _run(session, version="0.6.0", revision=12, status="passed")

    changed = supersede_stale_runs(session, user_id=1)

    assert changed == 1
    # Status is left as "running" — the runner's own report (update_job)
    # would otherwise blindly overwrite a directly-set status anyway.
    assert in_flight.status == "running"
    assert in_flight.cancel_requested is True


def test_different_architectures_are_independent_groups(db_session):
    session = db_session
    amd64_only = _run(session, version="0.5.0", revision=11, architecture="amd64")
    arm64_only = _run(session, version="0.4.0", revision=9, architecture="arm64")

    changed = supersede_stale_runs(session, user_id=1)

    assert changed == 0
    assert amd64_only.status == "passed"
    assert arm64_only.status == "passed"


def test_duplicate_same_version_runs_are_not_superseded(db_session):
    session = db_session
    first = _run(session, version="0.5.0", revision=11, status="failed")
    retry = _run(session, version="0.5.0", revision=11, status="passed")

    changed = supersede_stale_runs(session, user_id=1)

    assert changed == 0
    assert first.status == "failed"
    assert retry.status == "passed"


def test_already_promoted_run_is_never_touched(db_session):
    session = db_session
    promoted = _run(session, version="0.5.0", revision=11, status="promoted", promoted=True)
    _run(session, version="0.6.0", revision=12, status="passed")

    supersede_stale_runs(session, user_id=1)

    assert promoted.status == "promoted"


def test_scoped_to_one_snap_channel_architecture_when_given(db_session):
    session = db_session
    other_snap_old = _run(session, snap_name="filmcraft", version="0.4.0", revision=5)
    _run(session, snap_name="filmcraft", version="0.5.0", revision=6)
    pdfcraft_old = _run(session, version="0.5.0", revision=11)
    _run(session, version="0.6.0", revision=12)

    changed = supersede_stale_runs(
        session, user_id=1, snap_name="pdfcraft", from_channel="candidate", architecture="amd64",
    )

    assert changed == 1
    assert pdfcraft_old.status == "superseded"
    assert other_snap_old.status == "passed"  # out of scope, left alone
