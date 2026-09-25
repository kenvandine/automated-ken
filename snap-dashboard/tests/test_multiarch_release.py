"""Regression tests for multi-architecture runner dispatch and release sets.

- A runner must only claim jobs that belong to its own user and match its
  architecture (it fetches the suite with the job owner's GitHub token).
- Claiming resets ``started_at`` so the watchdog times the run, not the queue.
- A version bump's re-run supersedes the previous run for that architecture.
- A candidate release set is only auto-promotable when every architecture is
  approved, and a set can't be promoted twice.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from snap_dashboard.db.models import Base, ChannelMap, Runner, Snap, TestRun, User
from snap_dashboard.testing import orchestrator, release_set
from snap_dashboard.web.routes import runner_api


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

    for module in (runner_api, orchestrator, release_set):
        monkeypatch.setattr(module, "get_session", _fake_get_session)
    monkeypatch.setattr(runner_api, "get_user_config", lambda uid: SimpleNamespace(testing_repo=""))
    return _fake_get_session


def _users(session, n=2):
    users = [User(github_login=f"u{i}", github_id=i) for i in range(1, n + 1)]
    session.add_all(users)
    session.flush()
    return [u.id for u in users]


def _job(user_id, arch, **kw):
    fields = dict(
        snap_name="foo", architecture=arch, from_channel="edge", status="pending",
        dispatch_target="remote_runner", user_id=user_id,
    )
    fields.update(kw)
    return TestRun(**fields)


def test_runner_only_claims_own_users_jobs_of_its_arch(db):
    with db() as s:
        alice, bob = _users(s)
        runner = Runner(user_id=alice, name="a-arm", arch="arm64", secret_hash="h", status="idle")
        s.add(runner)
        s.add_all([_job(bob, "arm64"), _job(alice, "amd64"), _job(alice, "arm64")])
        s.flush()
        runner_id = runner.id

    job = runner_api._try_claim_job(runner_id)
    assert job is not None
    with db() as s:
        claimed = s.query(TestRun).get(job["test_run_id"])
        assert claimed.user_id == alice and claimed.architecture == "arm64"


def test_runner_does_not_claim_other_users_job_even_if_only_match(db):
    with db() as s:
        alice, bob = _users(s)
        runner = Runner(user_id=alice, name="a", arch="amd64", secret_hash="h", status="idle")
        s.add(runner)
        s.add(_job(bob, "amd64"))
        s.flush()
        runner_id = runner.id

    assert runner_api._try_claim_job(runner_id) is None


def test_claim_resets_started_at(db):
    queued_at = datetime.now(timezone.utc) - timedelta(hours=3)
    with db() as s:
        (alice,) = _users(s, 1)
        runner = Runner(user_id=alice, name="a", arch="amd64", secret_hash="h", status="idle")
        s.add(runner)
        s.add(_job(alice, "amd64", started_at=queued_at))
        s.flush()
        runner_id = runner.id

    job = runner_api._try_claim_job(runner_id)
    with db() as s:
        started = s.query(TestRun).get(job["test_run_id"]).started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    assert started > queued_at + timedelta(hours=2)


def test_latest_bump_runs_supersedes_rerun(db):
    with db() as s:
        (alice,) = _users(s, 1)
        old = _job(alice, "arm64", version_bump_pr_id=7, status="failed")
        amd = _job(alice, "amd64", version_bump_pr_id=7, status="passed")
        s.add_all([old, amd])
        s.flush()
        new = _job(alice, "arm64", version_bump_pr_id=7, status="passed")
        s.add(new)
        s.flush()
        runs = orchestrator.latest_bump_runs(s, 7)
        assert [(r.architecture, r.status) for r in runs] == [("amd64", "passed"), ("arm64", "passed")]


def _candidate_set(s, user_id, arm_review):
    snap = Snap(name="foo", user_id=user_id)
    s.add(snap)
    s.flush()
    for i, arch in enumerate(("amd64", "arm64", "armhf")):
        s.add(ChannelMap(snap_id=snap.id, channel="candidate", architecture=arch, version="2.0", revision=40 + i))
    common = dict(snap_name="foo", from_channel="candidate", version="2.0", status="passed", user_id=user_id)
    amd = TestRun(architecture="amd64", review_decision="approve", review_confidence=0.9, **common)
    arm = TestRun(architecture="arm64", review_decision=arm_review, review_confidence=0.9, **common)
    s.add_all([amd, arm])
    s.flush()
    return amd.id, arm.id


def test_release_set_waits_for_every_arch_and_ignores_untestable(db):
    with db() as s:
        (alice,) = _users(s, 1)
        _candidate_set(s, alice, arm_review="needs_review")
        members = release_set.candidate_release_set(s, alice, "foo", "2.0")
        assert [m["architecture"] for m in members] == ["amd64", "arm64"]  # armhf excluded
        states = [release_set.member_state(m, auto_threshold=0.85) for m in members]
        assert states == [release_set.READY, "review: needs_review"]


def test_release_set_promotes_all_together_once(db, monkeypatch):
    released = []
    monkeypatch.setattr(
        "snap_dashboard.testing.promoter.promote_snap",
        lambda name, rev, channel, store_credentials="": (released.append(rev) or True, "ok"),
    )
    monkeypatch.setattr(
        "snap_dashboard.testing.baselines.persist_stable_baseline_for_run", lambda *a, **k: 0
    )
    with db() as s:
        (alice,) = _users(s, 1)
        amd_id, arm_id = _candidate_set(s, alice, arm_review="approve")

    uc = SimpleNamespace(snapcraft_macaroon="m", github_token="", testing_repo="")
    promoted, failures = release_set.promote_release_set(alice, "foo", "2.0", [amd_id, arm_id], uc)
    assert sorted(promoted) == ["amd64", "arm64"] and not failures
    assert sorted(released) == [40, 41]

    again, _ = release_set.promote_release_set(alice, "foo", "2.0", [amd_id, arm_id], uc)
    assert again == [] and sorted(released) == [40, 41]


def test_promote_refuses_while_set_is_already_promoting(db):
    with db() as s:
        (alice,) = _users(s, 1)
        amd_id, arm_id = _candidate_set(s, alice, arm_review="approve")
        s.query(TestRun).get(arm_id).status = "promoting"

    uc = SimpleNamespace(snapcraft_macaroon="m", github_token="", testing_repo="")
    promoted, failures = release_set.promote_release_set(alice, "foo", "2.0", [amd_id, arm_id], uc)
    assert promoted == [] and "already in progress" in failures[0]
