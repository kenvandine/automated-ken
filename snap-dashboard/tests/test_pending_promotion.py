"""Regression test: a fully-promoted release set must not keep showing up
under "Pending Promotion".

``_build_pending_promotion`` scans the 50 most recent ``TestRun`` rows for
a passed, un-promoted candidate run to decide *which* (snap, version) sets
to build a card for. But a stale, superseded run row (e.g. an earlier
manual re-trigger) can remain "passed" and un-promoted in the database even
after the *current* run for every architecture has since been promoted to
stable. Since the actual per-architecture status always comes from
``candidate_release_set``/``member_state`` (which reflect the current
state, not the stale row), the card must be suppressed once every member
in that release set is already promoted — otherwise a "ready to promote"
card lingers forever with only a Dismiss button and nothing to actually
promote (see the reported screenshot: amd64 chip reads "promoted" but the
card still says "Ready to promote").
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from snap_dashboard.db.models import Base, ChannelMap, Snap, TestRun
from snap_dashboard.web.routes.testing import _build_pending_promotion


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_local()
    yield session
    session.close()


def _runs_data(runs):
    return [
        {
            "id": r.id,
            "snap_name": r.snap_name,
            "from_channel": r.from_channel,
            "version": r.version,
            "status": r.status,
            "promoted": r.promoted,
        }
        for r in runs
    ]


def test_stale_unpromoted_duplicate_is_hidden_once_the_set_is_fully_promoted(db_session):
    session = db_session
    snap = Snap(name="terminal-solitaire", user_id=1)
    session.add(snap)
    session.flush()
    session.add(
        ChannelMap(
            snap_id=snap.id, channel="candidate", architecture="amd64",
            version="0.0.1-19-gb0ebba8037", revision=42,
        )
    )

    # An earlier, superseded run for the same snap/version/arch that never
    # itself got marked promoted...
    stale = TestRun(
        user_id=1, snap_name="terminal-solitaire", architecture="amd64",
        from_channel="candidate", version="0.0.1-19-gb0ebba8037",
        revision=42, status="passed", promoted=False,
    )
    # ...but the current/latest run for that same architecture+version has
    # already been promoted to stable.
    current = TestRun(
        user_id=1, snap_name="terminal-solitaire", architecture="amd64",
        from_channel="candidate", version="0.0.1-19-gb0ebba8037",
        revision=42, status="passed", promoted=True,
    )
    session.add_all([stale, current])
    session.commit()

    # runs_data is most-recent-first, and the stale row happens to be seen
    # first in this scenario (e.g. it has a later started_at from a manual
    # re-trigger that predates the real promotion).
    runs_data = _runs_data([stale, current])

    pending = _build_pending_promotion(
        session, user_id=1, runs_data=runs_data,
        tracked_snap_names={"terminal-solitaire"}, dismissed=set(),
    )

    assert pending == []


def test_card_still_shows_when_something_remains_to_promote(db_session):
    session = db_session
    snap = Snap(name="terminal-fun", user_id=1)
    session.add(snap)
    session.flush()
    session.add(
        ChannelMap(
            snap_id=snap.id, channel="candidate", architecture="amd64",
            version="0+git.7507c34", revision=7,
        )
    )

    run = TestRun(
        user_id=1, snap_name="terminal-fun", architecture="amd64",
        from_channel="candidate", version="0+git.7507c34",
        revision=7, status="passed", promoted=False,
    )
    session.add(run)
    session.commit()

    runs_data = _runs_data([run])

    pending = _build_pending_promotion(
        session, user_id=1, runs_data=runs_data,
        tracked_snap_names={"terminal-fun"}, dismissed=set(),
    )

    assert len(pending) == 1
    assert pending[0]["snap_name"] == "terminal-fun"
    assert pending[0]["members"][0]["state"] == "ready"
