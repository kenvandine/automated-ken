"""Tests for the /snaps page and the snap-management actions that moved
there from Settings (remove, fleet actions) — including the legacy
/settings/* aliases."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from snap_dashboard.db.models import (
    Base,
    ChannelMap,
    Issue,
    PromotionDismissal,
    Snap,
    StableScreenshotBaseline,
    TestRun,
    User,
    UserConfig,
    VersionBumpPR,
)
from snap_dashboard.web import fleet_actions
from snap_dashboard.web.fleet import build_snap_rows
from snap_dashboard.web.routes import settings as settings_module
from snap_dashboard.web.routes import snaps as snaps_module


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

    import snap_dashboard.auth as auth_module

    for mod in (auth_module, snaps_module, settings_module):
        monkeypatch.setattr(mod, "get_session", _fake_get_session)
    return session_local


def _request(user_id: int, path: str = "/snaps", fetch: bool = False) -> Request:
    headers = [(b"x-requested-with", b"fetch")] if fetch else []
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "query_string": b"",
            "headers": headers,
            "session": {"user_id": user_id},
            "app": None,
        }
    )


def _seed(session_local) -> int:
    s = session_local()
    user = User(github_login="ken", github_id=1)
    s.add(user)
    s.flush()
    s.add(UserConfig(user_id=user.id, publisher="ken", github_token="gh"))
    a = Snap(name="alpha", user_id=user.id, packaging_repo="https://github.com/k/alpha")
    b = Snap(name="beta", user_id=user.id, is_console_app=True)
    s.add_all([a, b])
    s.flush()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    s.add_all(
        [
            ChannelMap(snap_id=a.id, channel="stable", architecture="amd64", version="1.0", fetched_at=now),
            ChannelMap(snap_id=a.id, channel="candidate", architecture="amd64", version="1.1", fetched_at=now),
            ChannelMap(snap_id=a.id, channel="edge", architecture="arm64", version="1.2", fetched_at=now),
            Issue(snap_id=a.id, repo_url="r", issue_number=1, state="open", type="issue"),
            Issue(snap_id=a.id, repo_url="r", issue_number=2, state="open", type="issue"),
            Issue(snap_id=a.id, repo_url="r", issue_number=3, state="open", type="pr"),
            Issue(snap_id=a.id, repo_url="r", issue_number=4, state="closed", type="pr"),
            TestRun(user_id=user.id, snap_name="alpha", from_channel="candidate", status="passed"),
            TestRun(user_id=user.id, snap_name="alpha", from_channel="candidate", status="failed"),
            TestRun(user_id=user.id, snap_name="beta", from_channel="edge", status="passed"),
            VersionBumpPR(snap_id=a.id, user_id=user.id, status="needs_review", new_version="1.1"),
            StableScreenshotBaseline(user_id=user.id, snap_name="alpha", architecture="amd64", image_name="x.png", image_b64=""),
            PromotionDismissal(user_id=user.id, snap_name="alpha", version="1.1"),
        ]
    )
    s.commit()
    uid = user.id
    s.close()
    return uid


def test_build_snap_rows_batches_everything(db):
    uid = _seed(db)
    s = db()
    rows = {r["name"]: r for r in build_snap_rows(s, uid)}
    s.close()

    alpha = rows["alpha"]
    assert alpha["stable"] == "1.0"
    assert alpha["ahead"] == {"channel": "candidate", "version": "1.1"}
    assert alpha["arches"] == ["amd64", "arm64"]
    assert alpha["issue_count"] == 2 and alpha["pr_count"] == 1
    assert alpha["latest_run"]["status"] == "failed"
    assert alpha["bump"]["status"] == "needs_review"
    assert "Tests failed" in alpha["attention"]
    assert "Bump PR needs you" in alpha["attention"]

    beta = rows["beta"]
    assert beta["type"] == "console"
    assert beta["stable"] is None
    assert "No packaging repo" in beta["attention"]


def test_snaps_page_renders(db, monkeypatch):
    uid = _seed(db)
    import snap_dashboard.auth as auth_module

    monkeypatch.setattr(snaps_module, "get_user_config", auth_module.get_user_config)
    req = _request(uid)
    req.scope["method"] = "GET"
    resp = snaps_module.snaps_index(req)
    html = resp.body.decode()
    assert 'data-snap="alpha"' in html and 'data-snap="beta"' in html
    assert 'action="/snap/alpha/remove"' in html
    assert 'action="/snaps/actions/rebuild-all"' in html
    assert 'id="add"' in html


def _remaining(session_local, uid):
    s = session_local()
    out = {
        "snaps": sorted(x.name for x in s.query(Snap).filter_by(user_id=uid)),
        "runs": s.query(TestRun).filter_by(snap_name="alpha").count(),
        "baselines": s.query(StableScreenshotBaseline).filter_by(snap_name="alpha").count(),
        "dismissals": s.query(PromotionDismissal).filter_by(snap_name="alpha").count(),
        "beta_runs": s.query(TestRun).filter_by(snap_name="beta").count(),
    }
    s.close()
    return out


def test_remove_snap_route_cascades(db):
    uid = _seed(db)
    resp = asyncio.run(snaps_module.snap_remove("alpha", _request(uid, fetch=True)))
    body = json.loads(resp.body)
    assert body["ok"] is True and body["removed"] == "alpha"
    assert _remaining(db, uid) == {"snaps": ["beta"], "runs": 0, "baselines": 0, "dismissals": 0, "beta_runs": 1}


def test_remove_unknown_snap_is_404(db):
    uid = _seed(db)
    resp = asyncio.run(snaps_module.snap_remove("nope", _request(uid, fetch=True)))
    assert resp.status_code == 404
    assert json.loads(resp.body)["ok"] is False


def test_legacy_settings_remove_alias(db):
    uid = _seed(db)
    resp = asyncio.run(settings_module.settings_remove_snap("alpha", _request(uid)))
    assert resp.status_code == 303 and resp.headers["location"] == "/snaps"
    assert _remaining(db, uid)["snaps"] == ["beta"]


def test_fleet_action_checks_preconditions(db, monkeypatch):
    uid = _seed(db)
    submitted = []
    monkeypatch.setattr(fleet_actions, "_make_agent", lambda action, user_id: submitted.append(action))
    import snap_dashboard.auth as auth_module

    monkeypatch.setattr(fleet_actions, "get_user_config", auth_module.get_user_config)

    ok = asyncio.run(snaps_module.snaps_fleet_action("rebuild-all", _request(uid, fetch=True)))
    assert json.loads(ok.body)["ok"] is True
    assert submitted == ["rebuild-all"]

    # No Store credential configured → refused, nothing submitted.
    refused = asyncio.run(settings_module.settings_sync_snapcraft_credentials(_request(uid, fetch=True)))
    assert refused.status_code == 400
    assert json.loads(refused.body)["error"] == "no_snapcraft_credential"

    # Plain form post falls back to a redirect back to /snaps.
    redirect = asyncio.run(settings_module.settings_run_fleet_normalization(_request(uid)))
    assert redirect.headers["location"] == "/snaps?error=fleet_normalization_disabled"
    assert submitted == ["rebuild-all"]

    unknown = asyncio.run(snaps_module.snaps_fleet_action("bogus", _request(uid, fetch=True)))
    assert unknown.status_code == 404


def test_snap_add_get_redirects_to_inline_panel(db):
    resp = asyncio.run(snaps_module.snap_add_get(_request(1)))
    assert resp.headers["location"] == "/snaps#add"


def test_edit_snap_type_radio(db):
    uid = _seed(db)

    async def _edit(**kw):
        return await snaps_module.snap_edit(
            request=_request(uid), name="alpha", packaging_repo="https://github.com/k/alpha",
            upstream_repo="", notes="", **kw,
        )

    asyncio.run(_edit(is_console_app="", is_service="", snap_type="service"))
    s = db()
    snap = s.query(Snap).filter_by(name="alpha").one()
    assert snap.is_service is True and snap.is_console_app is False
    s.close()

    asyncio.run(_edit(is_console_app="", is_service="", snap_type="console"))
    s = db()
    snap = s.query(Snap).filter_by(name="alpha").one()
    assert snap.is_service is False and snap.is_console_app is True
    s.close()
