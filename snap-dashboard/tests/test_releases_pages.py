"""Tests for the Releases section (/releases, /releases/bumps,
/releases/runs) and the legacy /testing and /version-bumps redirects."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from snap_dashboard.db.models import Base, Snap, TestRun, User, UserConfig, VersionBumpPR
from snap_dashboard.web.routes import testing as testing_module
from snap_dashboard.web.routes import version_bumps as bumps_module


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
        finally:
            session.close()

    import snap_dashboard.auth as auth_module

    for mod in (auth_module, testing_module, bumps_module):
        monkeypatch.setattr(mod, "get_session", _fake_get_session)
    monkeypatch.setattr(testing_module, "get_user_config", auth_module.get_user_config)
    return session_local


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _seed(session_local) -> int:
    s = session_local()
    user = User(github_login="ken", github_id=1)
    s.add(user)
    s.flush()
    s.add(UserConfig(user_id=user.id, publisher="ken", testing_repo="ken/tests"))
    snap = Snap(name="alpha", user_id=user.id, packaging_repo="https://github.com/k/alpha")
    s.add(snap)
    s.flush()
    s.add_all([
        TestRun(user_id=user.id, snap_name="alpha", version="2.0", architecture="amd64",
                from_channel="candidate", status="failed", started_at=_now(),
                failure_analysis="Window never appeared"),
        TestRun(user_id=user.id, snap_name="alpha", version="2.0", architecture="arm64",
                from_channel="candidate", status="running", started_at=_now()),
        VersionBumpPR(snap_id=snap.id, user_id=user.id, status="needs_review", new_version="2.1",
                      old_version="2.0", bot_pr_url="https://github.com/k/alpha/pull/9", bot_pr_number=9),
        VersionBumpPR(snap_id=snap.id, user_id=user.id, status="stable_promoted", new_version="1.9",
                      old_version="1.8"),
    ])
    s.commit()
    uid = user.id
    s.close()
    return uid


def _get(uid: int, path: str) -> Request:
    return Request({
        "type": "http", "method": "GET", "path": path, "query_string": b"",
        "headers": [], "session": {"user_id": uid}, "app": None,
    })


def test_legacy_urls_redirect_permanently():
    r1 = asyncio.run(testing_module.testing_legacy())
    r2 = asyncio.run(bumps_module.version_bumps_legacy())
    assert (r1.status_code, r1.headers["location"]) == (301, "/releases")
    assert (r2.status_code, r2.headers["location"]) == (301, "/releases/bumps")


def test_releases_queue_shows_promotions_and_needing_tests(db, monkeypatch):
    uid = _seed(db)
    item = {
        "snap": SimpleNamespace(name="alpha", packaging_repo="https://github.com/k/alpha"),
        "architectures": ["amd64", "arm64"],
        "from_channel": "candidate",
        "version": "2.0",
        "revisions": {"amd64": 10, "arm64": 11},
        "stable_ver": {"amd64": "1.9", "arm64": "1.9"},
        "can_promote": True,
    }
    monkeypatch.setattr(testing_module, "find_snaps_needing_tests", lambda s, user_id: [item])
    monkeypatch.setattr(testing_module, "candidate_release_set", lambda *a: [])
    monkeypatch.setattr(
        testing_module, "_build_pending_promotion",
        lambda *a: [{
            "snap_name": "alpha", "version": "1.9",
            "members": [
                {"architecture": "amd64", "state": "ready", "run_id": 1,
                 "review_decision": "approve", "review_confidence": 0.9},
                {"architecture": "arm64", "state": "failed", "run_id": 2,
                 "review_decision": None, "review_confidence": None},
            ],
        }],
    )
    resp = asyncio.run(testing_module.testing_index(_get(uid, "/releases")))
    assert resp.status_code == 200
    body = resp.body.decode()
    assert 'id="promote"' in body
    assert "Partly ready" in body
    assert "Promote anyway (skip arm64)" in body
    assert "Candidate — test &amp; promote" in body
    assert 'class="js-trigger-form"' in body
    # Running arm64 run is a live chip the page polls; failed amd64 is static.
    assert 'js-run-status" data-run-id=' in body
    assert "chip--negative" in body
    # Tabs with the bumps-needing-you badge.
    assert 'href="/releases/bumps"' in body and 'tab-bar__count">1<' in body
    assert "Recent test runs" not in body


def test_releases_runs_view(db):
    uid = _seed(db)
    resp = asyncio.run(testing_module.releases_runs(_get(uid, "/releases/runs")))
    assert resp.status_code == 200
    body = resp.body.decode()
    assert "Recent test runs" in body
    assert 'title="Window never appeared"' in body
    assert 'action="/testing/runs/' in body  # mark-failed for the running run
    assert 'href="/releases/runs" class="tab-bar__tab is-active"' in body


def test_releases_empty_state_without_runs(db, monkeypatch):
    s = db()
    user = User(github_login="new", github_id=5)
    s.add(user)
    s.flush()
    s.add(UserConfig(user_id=user.id))
    s.commit()
    uid = user.id
    s.close()
    resp = asyncio.run(testing_module.testing_index(_get(uid, "/releases")))
    body = resp.body.decode()
    assert "Nothing to test yet" in body
    assert 'href="/runners"' in body


def test_bumps_page_orders_needs_you_first(db):
    uid = _seed(db)
    resp = asyncio.run(bumps_module.version_bumps_page(_get(uid, "/releases/bumps")))
    assert resp.status_code == 200
    body = resp.body.decode()
    assert body.index('id="group-needs_review"') < body.index('id="group-stable_promoted"')
    assert "card--tone-caution" in body
    assert 'href="/releases/bumps" class="tab-bar__tab is-active"' in body



def test_releases_runs_view_skips_queue_work(db, monkeypatch):
    uid = _seed(db)

    def _boom(*a, **k):
        raise AssertionError("queue-only work ran for the runs tab")

    monkeypatch.setattr(testing_module, "find_snaps_needing_tests", _boom)
    monkeypatch.setattr(testing_module, "_build_pending_promotion", _boom)
    resp = asyncio.run(testing_module.releases_runs(_get(uid, "/releases/runs")))
    assert resp.status_code == 200


def test_snaps_add_redirects_permanently():
    from snap_dashboard.web.routes import snaps as snaps_module

    r = asyncio.run(snaps_module.snap_add_get(_get(1, "/snaps/add")))
    assert (r.status_code, r.headers["location"]) == (301, "/snaps#add")


def test_task_failure_statuses_are_negative():
    from snap_dashboard.web.templating import status_info

    assert status_info("dispatch_failed")["tone"] == "negative"
    assert status_info("timed_out")["tone"] == "negative"


def test_overview_does_not_list_promoted_bump_twice(db):
    from snap_dashboard.web import fleet

    uid = _seed(db)
    s = db()
    s.add(TestRun(user_id=uid, snap_name="alpha", version="1.9", architecture="amd64",
                  from_channel="candidate", status="passed", started_at=_now(),
                  promoted=True, promoted_at=_now()))
    s.query(VersionBumpPR).filter_by(new_version="1.9").update({"updated_at": _now()})
    s.commit()
    overview = fleet.build_overview(s, uid, rows=[])
    s.close()
    assert [(x["snap"], x["version"]) for x in overview["shipped"]] == [("alpha", "1.9")]
    assert overview["shipped_bumps"] == []
