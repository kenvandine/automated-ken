"""Regression test: find_snaps_needing_tests() must compare versions, not
just check inequality.

``find_snaps_needing_tests`` used to flag a candidate/edge entry as
"needs testing" whenever its version merely *differed* from stable (and,
for edge, from candidate) — not whenever it was actually *newer*. That
meant a snap whose edge channel lagged behind a stable/candidate release
that had since moved on (e.g. edge=0.5.1 while stable/candidate are both
already at 0.5.2) incorrectly showed up under "Edge — Test Only" forever,
with no way to clear it since edge was never rebuilt.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from snap_dashboard.db.models import Base, ChannelMap, Snap
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


def _seed_channel_map(session, snap_id, channel, arch, version, revision):
    session.add(
        ChannelMap(
            snap_id=snap_id,
            channel=channel,
            architecture=arch,
            version=version,
            revision=revision,
        )
    )


def test_stale_edge_older_than_stable_and_candidate_is_not_flagged(isolated_session):
    """Matches the reported bug: stable=candidate=0.5.2, edge=0.5.1 (older)."""
    session_local = isolated_session
    with session_local() as session:
        snap = Snap(name="fresh-editor", user_id=1)
        session.add(snap)
        session.flush()
        for arch, r_stable, r_candidate, r_edge in (
            ("amd64", 17, 17, 12),
            ("arm64", 16, 16, 11),
        ):
            _seed_channel_map(session, snap.id, "stable", arch, "0.5.2", r_stable)
            _seed_channel_map(session, snap.id, "candidate", arch, "0.5.2", r_candidate)
            _seed_channel_map(session, snap.id, "edge", arch, "0.5.1", r_edge)
        session.commit()

        results = orchestrator.find_snaps_needing_tests(session, user_id=1)

    assert results == []


def test_stale_candidate_older_than_stable_is_not_flagged(isolated_session):
    """A candidate that regressed behind stable must not show as promotable."""
    session_local = isolated_session
    with session_local() as session:
        snap = Snap(name="regressed-app", user_id=1)
        session.add(snap)
        session.flush()
        _seed_channel_map(session, snap.id, "stable", "amd64", "2.0.0", 10)
        _seed_channel_map(session, snap.id, "candidate", "amd64", "1.9.0", 8)
        session.commit()

        results = orchestrator.find_snaps_needing_tests(session, user_id=1)

    assert results == []


def test_edge_newer_than_both_stable_and_candidate_is_still_flagged(isolated_session):
    """The legitimate case must keep working: edge genuinely ahead of both."""
    session_local = isolated_session
    with session_local() as session:
        snap = Snap(name="fresh-editor", user_id=1)
        session.add(snap)
        session.flush()
        _seed_channel_map(session, snap.id, "stable", "amd64", "0.5.1", 10)
        _seed_channel_map(session, snap.id, "candidate", "amd64", "0.5.1", 10)
        _seed_channel_map(session, snap.id, "edge", "amd64", "0.5.2", 12)
        session.commit()

        results = orchestrator.find_snaps_needing_tests(session, user_id=1)

    assert len(results) == 1
    assert results[0]["from_channel"] == "edge"
    assert results[0]["can_promote"] is False
    assert results[0]["version"] == "0.5.2"


def test_candidate_newer_than_stable_is_still_flagged(isolated_session):
    """The legitimate promotion case must keep working."""
    session_local = isolated_session
    with session_local() as session:
        snap = Snap(name="fresh-editor", user_id=1)
        session.add(snap)
        session.flush()
        _seed_channel_map(session, snap.id, "stable", "amd64", "0.5.1", 10)
        _seed_channel_map(session, snap.id, "candidate", "amd64", "0.5.2", 12)
        session.commit()

        results = orchestrator.find_snaps_needing_tests(session, user_id=1)

    assert len(results) == 1
    assert results[0]["from_channel"] == "candidate"
    assert results[0]["can_promote"] is True
    assert results[0]["version"] == "0.5.2"


def test_revision_is_authoritative_over_an_unsortable_version_string(isolated_session):
    """Revision must win even when the version string can't be compared.

    Snap versions are arbitrary, upstream-controlled strings (dates, git
    hashes, build metadata, ...) that don't necessarily sort the way a
    naive string/semver comparison would suggest. Revisions, by contrast,
    are Store-assigned and strictly increasing on every publish — so a
    higher revision must always win regardless of what the version string
    looks like.
    """
    session_local = isolated_session
    with session_local() as session:
        snap = Snap(name="git-hash-versioned-app", user_id=1)
        session.add(snap)
        session.flush()
        # "abc123" sorts lexicographically *after* "zzz999" is false, but
        # pick a pair where a naive comparator would get it backwards:
        # candidate's version string is "lexicographically smaller" than
        # stable's, yet its revision (the real source of truth) is higher.
        _seed_channel_map(session, snap.id, "stable", "amd64", "zzz-build", 10)
        _seed_channel_map(session, snap.id, "candidate", "amd64", "aaa-build", 15)
        session.commit()

        results = orchestrator.find_snaps_needing_tests(session, user_id=1)

    assert len(results) == 1
    assert results[0]["from_channel"] == "candidate"
    assert results[0]["version"] == "aaa-build"


def test_same_version_higher_revision_rebuild_still_flagged(isolated_session):
    """Same-version security rebuild (higher revision) must still show up."""
    session_local = isolated_session
    with session_local() as session:
        snap = Snap(name="fresh-editor", user_id=1)
        session.add(snap)
        session.flush()
        _seed_channel_map(session, snap.id, "stable", "amd64", "0.5.1", 10)
        _seed_channel_map(session, snap.id, "candidate", "amd64", "0.5.1", 11)
        session.commit()

        results = orchestrator.find_snaps_needing_tests(session, user_id=1)

    assert len(results) == 1
    assert results[0]["from_channel"] == "candidate"
