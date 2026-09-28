"""Regression test: a candidate rebuild (same version, new revision) must
still be auto-tested.

``queue_auto_tests`` used to dedup only on
snap/channel/version/architecture, so once a version had *any* TestRun
recorded, a later security rebuild published to candidate under the same
version string (but a new Store revision) was silently treated as
"already tested" and never queued — even though ``find_snaps_needing_tests``
correctly flags it as needing a test via its revision-based "Include
rebuilds" check.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from snap_dashboard.db.models import Base, ChannelMap, Snap, TestRun
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


def _seed_snap_with_channels(session_local, *, stable_rev, candidate_rev, version):
    with session_local() as session:
        snap = Snap(name="gedit", user_id=1)
        session.add(snap)
        session.flush()
        session.add_all(
            [
                ChannelMap(
                    snap_id=snap.id,
                    channel="stable",
                    architecture="amd64",
                    version=version,
                    revision=stable_rev,
                ),
                ChannelMap(
                    snap_id=snap.id,
                    channel="candidate",
                    architecture="amd64",
                    version=version,
                    revision=candidate_rev,
                ),
            ]
        )
        session.commit()


def test_queue_auto_tests_queues_a_rebuild_with_same_version_new_revision(
    monkeypatch, isolated_session
):
    session_local = isolated_session

    # candidate is a rebuild of the currently-stable version: same version
    # string, higher revision (e.g. a security rebuild).
    _seed_snap_with_channels(session_local, stable_rev=100, candidate_rev=105, version="46.0")

    # A prior TestRun already exists for this exact version, tied to the
    # *old* revision (it was tested before it was promoted to stable).
    with session_local() as session:
        session.add(
            TestRun(
                user_id=1,
                snap_name="gedit",
                architecture="amd64",
                from_channel="candidate",
                version="46.0",
                revision=100,
                status="passed",
            )
        )
        session.commit()

    triggered = []

    def _fake_trigger_remote_run(snap_name, channel, version, revision, architecture, triggered_by, user_id):
        triggered.append((snap_name, channel, version, revision, architecture))
        return True, None, 999

    monkeypatch.setattr(orchestrator, "trigger_remote_run", _fake_trigger_remote_run)

    queued = orchestrator.queue_auto_tests(user_id=1)

    assert queued == 1
    assert triggered == [("gedit", "candidate", "46.0", 105, "amd64")]


def test_queue_auto_tests_skips_when_same_revision_already_tested(monkeypatch, isolated_session):
    session_local = isolated_session

    _seed_snap_with_channels(session_local, stable_rev=100, candidate_rev=105, version="46.0")

    with session_local() as session:
        session.add(
            TestRun(
                user_id=1,
                snap_name="gedit",
                architecture="amd64",
                from_channel="candidate",
                version="46.0",
                revision=105,
                status="passed",
            )
        )
        session.commit()

    triggered = []
    monkeypatch.setattr(
        orchestrator,
        "trigger_remote_run",
        lambda *a, **k: triggered.append(a) or (True, None, 999),
    )

    queued = orchestrator.queue_auto_tests(user_id=1)

    assert queued == 0
    assert triggered == []
