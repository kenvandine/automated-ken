"""Tests for manually rejecting a candidate run's screenshot review.

A user viewing a pending promotion's screenshot should be able to reject
it themselves (``POST /testing/runs/{id}/reject``), overriding whatever
the AI reviewer decided. Two things must then hold:

1. A manual rejection blocks promotion exactly like an AI "reject" does
   (``testing/release_set.py:member_state``).
2. Because a stable baseline is only ever persisted from a run that
   actually got *promoted* (see ``testing/baselines.py``), a rejected run
   can never later be picked up as a "known good" reference screenshot.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from snap_dashboard.db.models import Base, TestRun
from snap_dashboard.testing import baselines as baselines_mod
from snap_dashboard.testing.release_set import READY, member_state
from snap_dashboard.web.routes import testing as testing_routes


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

    monkeypatch.setattr(testing_routes, "get_session", _fake_get_session)
    monkeypatch.setattr(baselines_mod, "get_session", _fake_get_session)
    return session_local


def test_manual_reject_route_records_decision_and_blocks_promotion(monkeypatch, isolated_session):
    session_local = isolated_session

    with session_local() as session:
        run = TestRun(
            user_id=1, snap_name="evince", architecture="amd64",
            from_channel="candidate", version="45.0", revision=17,
            status="passed", review_decision="approve", review_confidence=0.9,
        )
        session.add(run)
        session.commit()
        run_id = run.id

    monkeypatch.setattr(
        testing_routes, "get_current_user",
        lambda request: {"id": 1, "display_name": "Ken", "github_login": "kenvandine"},
    )

    response = testing_routes.reject_run_review(
        run_id, request=None, return_to=f"/testing/runs/{run_id}"
    )
    assert response.status_code == 303

    with session_local() as session:
        updated = session.query(TestRun).get(run_id)
        assert updated.review_decision == "reject"
        assert "Ken" in updated.review_reasoning
        assert updated.promoted is False

        member = {
            "promoted": False, "run": updated, "revision": updated.revision,
            "architecture": updated.architecture,
        }
        assert member_state(member) == "review: rejected"
        assert member_state(member) != READY


def test_promoted_run_cannot_be_manually_rejected(monkeypatch, isolated_session):
    session_local = isolated_session

    with session_local() as session:
        run = TestRun(
            user_id=1, snap_name="evince", architecture="amd64",
            from_channel="candidate", version="45.0", revision=17,
            status="promoted", promoted=True, review_decision="approve",
        )
        session.add(run)
        session.commit()
        run_id = run.id

    monkeypatch.setattr(
        testing_routes, "get_current_user",
        lambda request: {"id": 1, "display_name": "Ken", "github_login": "kenvandine"},
    )

    testing_routes.reject_run_review(run_id, request=None, return_to="/testing")

    with session_local() as session:
        updated = session.query(TestRun).get(run_id)
        # Already promoted — the manual reject is a no-op, since the
        # release already happened.
        assert updated.review_decision == "approve"


def test_rejected_unpromoted_run_never_becomes_a_stable_baseline(isolated_session):
    session_local = isolated_session

    with session_local() as session:
        rejected = TestRun(
            user_id=1, snap_name="evince", architecture="amd64",
            from_channel="candidate", version="45.0", revision=17,
            status="passed", promoted=False, review_decision="reject",
        )
        session.add(rejected)
        session.commit()

    # get_or_build_stable_baseline_assets() only looks up runs with
    # promoted=True to backfill from — a rejected, unpromoted run must
    # never be picked up as a reference, no matter how recent it is.
    assets = baselines_mod.get_or_build_stable_baseline_assets(
        user_id=1, snap_name="evince", architecture="amd64", testing_repo="owner/repo",
    )
    assert assets == []
