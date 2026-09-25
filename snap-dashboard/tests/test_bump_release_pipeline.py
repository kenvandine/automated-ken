"""Regression tests for version bumps continuing from merge to a stable release.

Pre-merge edge tests only gate the merge. With auto-promote on, a merged
bump waits for its version to reach candidate (releasing it from edge if
CI only published it there), tests the candidate set per architecture, and
promotes the whole set — or stops for review if any architecture fails.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from snap_dashboard.agents import pr_monitor
from snap_dashboard.db.models import Base, ChannelMap, Snap, TestRun, User, VersionBumpPR
from snap_dashboard.testing import orchestrator, release_set


@pytest.fixture
def env(monkeypatch):
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

    for module in (pr_monitor, orchestrator, release_set):
        monkeypatch.setattr(module, "get_session", _fake_get_session)

    uc = SimpleNamespace(
        auto_promote=True, auto_promote_confidence=0.85, snapcraft_macaroon="m",
        github_token="", testing_repo="",
    )
    monkeypatch.setattr(pr_monitor, "get_user_config", lambda uid: uc)

    released: list[tuple[int, str]] = []
    store: dict[tuple[str, str], int] = {}  # (channel, arch) -> revision, for version 2.0

    def _promote(name, revision, channel, store_credentials=""):
        released.append((revision, channel))
        arch = next(a for (ch, a), r in store.items() if r == revision)
        store[(channel, arch)] = revision
        return True, "ok"

    def _refresh(snap_id, snap_name):
        with _fake_get_session() as s:
            s.query(ChannelMap).filter_by(snap_id=snap_id, version="2.0").delete()
            for (channel, arch), rev in store.items():
                s.add(ChannelMap(snap_id=snap_id, channel=channel, architecture=arch, version="2.0", revision=rev))
        return True

    monkeypatch.setattr("snap_dashboard.testing.promoter.promote_snap", _promote)
    monkeypatch.setattr("snap_dashboard.collector.refresh_channel_map", _refresh)
    monkeypatch.setattr("snap_dashboard.testing.baselines.persist_stable_baseline_for_run", lambda *a, **k: 0)

    with _fake_get_session() as s:
        user = User(github_login="k", github_id=1)
        s.add(user)
        s.flush()
        snap = Snap(name="foo", user_id=user.id)
        s.add(snap)
        s.flush()
        for arch in ("amd64", "arm64"):  # the snap ships both, currently 1.0 in stable
            s.add(ChannelMap(snap_id=snap.id, channel="stable", architecture=arch, version="1.0", revision=1))
        bump = VersionBumpPR(
            snap_id=snap.id, user_id=user.id, old_version="1.0", new_version="2.0",
            status="agent_approved", packaging_repo="https://github.com/k/foo", bot_pr_number=5,
        )
        s.add(bump)
        s.flush()
        ids = SimpleNamespace(user=user.id, snap=snap.id, bump=bump.id)

    return SimpleNamespace(db=_fake_get_session, uc=uc, released=released, store=store, ids=ids)


def _bump(env):
    with env.db() as s:
        return s.query(VersionBumpPR).get(env.ids.bump)


def _advance(env):
    b = _bump(env)
    pr = {
        "id": b.id, "snap_id": b.snap_id, "user_id": b.user_id, "status": b.status,
        "packaging_repo": b.packaging_repo, "bot_pr_number": b.bot_pr_number,
        "bot_pr_url": "", "branch_name": "", "test_run_id": b.test_run_id,
        "new_version": b.new_version, "old_version": b.old_version,
        "merged": b.merged_at is not None, "snap_name": "foo",
    }
    return pr_monitor.PRMonitorAgent()._advance(pr)


def _candidate_runs(env):
    with env.db() as s:
        return {
            r.architecture: r for r in
            s.query(TestRun).filter_by(snap_name="foo", from_channel="candidate", version="2.0")
        }


def _finish(env, arch, status="passed", decision="approve"):
    with env.db() as s:
        run = s.query(TestRun).filter_by(snap_name="foo", architecture=arch, from_channel="candidate").first()
        run.status = status
        run.review_decision = decision if status == "passed" else None
        run.review_confidence = 0.9


def test_merge_goes_to_awaiting_release_only_with_auto_promote(env):
    pr_monitor.mark_bump_merged(env.ids.bump)
    b = _bump(env)
    assert b.status == "awaiting_release" and b.merged_at is not None

    env.uc.auto_promote = False
    with env.db() as s:
        s.query(VersionBumpPR).get(env.ids.bump).merged_at = None
    pr_monitor.mark_bump_merged(env.ids.bump)
    assert _bump(env).status == "merged"


def test_waits_until_every_arch_is_published(env):
    pr_monitor.mark_bump_merged(env.ids.bump)
    env.store[("edge", "amd64")] = 10  # arm64 not built yet
    assert _advance(env) is False
    assert env.released == [] and _bump(env).status == "awaiting_release"


def test_releases_edge_to_candidate_tests_and_promotes_the_set(env):
    pr_monitor.mark_bump_merged(env.ids.bump)
    env.store[("edge", "amd64")] = 10
    env.store[("edge", "arm64")] = 11

    assert _advance(env) is True
    assert sorted(env.released) == [(10, "candidate"), (11, "candidate")]
    assert _bump(env).status == "candidate_testing"
    assert set(_candidate_runs(env)) == {"amd64", "arm64"}

    _finish(env, "amd64")
    _advance(env)  # arm64 still pending → nothing promoted yet
    assert (10, "stable") not in env.released

    _finish(env, "arm64")
    _advance(env)
    assert {(10, "stable"), (11, "stable")} <= set(env.released)
    assert _bump(env).status == "stable_promoted"


def test_failed_arch_stops_for_review_without_promoting(env):
    with env.db() as s:
        s.query(VersionBumpPR).get(env.ids.bump).merged_at = datetime.now(timezone.utc)
        s.query(VersionBumpPR).get(env.ids.bump).status = "awaiting_release"
    env.store[("candidate", "amd64")] = 20  # CI published straight to candidate
    env.store[("candidate", "arm64")] = 21

    _advance(env)
    assert env.released == [] and _bump(env).status == "candidate_testing"

    _finish(env, "amd64")
    _finish(env, "arm64", status="failed")
    _advance(env)
    b = _bump(env)
    assert b.status == "needs_review" and "arm64 (failed)" in b.agent_reasoning
    assert not any(ch == "stable" for _, ch in env.released)
