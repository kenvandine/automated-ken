"""Regression tests for delegating version bumps to a coding agent.

``VersionBumperAgent`` used to patch ``snapcraft.yaml`` itself with a
regex-based line patch. That's now delegated to whichever "capable coding"
backend is configured (see ``agents/coding_backend.py``) — the regex patch
only survives as a last-resort fallback when no coding backend is
available at all. These tests cover: the coding-agent dispatch path for
both a synchronous backend (local Lemonade — resolves immediately) and an
async one (GitHub Copilot cloud agent — leaves the bump "dispatched" for
``pr_monitor.py`` to poll later), a dispatch failure, and the legacy
fallback when no backend is configured.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from snap_dashboard.agents import version_bumper as vb
from snap_dashboard.db.models import Base, Snap, UpstreamRelease, User, VersionBumpPR


@pytest.fixture
def isolated_session(monkeypatch):
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

    monkeypatch.setattr(vb, "get_session", _fake_get_session)
    return session_local


def _seed(session_local) -> int:
    """Insert a User + Snap + UpstreamRelease, return the release id."""
    with session_local() as session:
        session.add(User(id=1, github_login="kenvandine", github_id=1))
        snap = Snap(name="godot-4", packaging_repo="https://github.com/kenvandine/godot-snap")
        session.add(snap)
        session.flush()
        release = UpstreamRelease(
            snap_id=snap.id, part_name="godot", latest_version="4.3", current_version="4.2",
        )
        session.add(release)
        session.commit()
        return release.id


class _FakeBotClient:
    def __init__(self):
        self.head_branch = "coding-agent/version-bump"

    def get_default_branch(self, owner, repo):
        return "main"

    def get_pr_head_branch(self, owner, repo, pr_number):
        return self.head_branch


def _agent(release_id: int, **overrides) -> vb.VersionBumperAgent:
    kwargs = dict(
        snap_id=1,
        snap_name="godot-4",
        packaging_repo="https://github.com/kenvandine/godot-snap",
        upstream_release_id=release_id,
        part_name="godot",
        old_version="4.2",
        new_version="4.3",
        release_url="https://github.com/kenvandine/godot/releases/tag/4.3",
        user_id=1,
    )
    kwargs.update(overrides)
    return vb.VersionBumperAgent(**kwargs)


def _patch_common(monkeypatch, bot_token="bot-token", found=("snap/snapcraft.yaml", "content", "sha1")):
    class _UC:
        bot_github_token = bot_token
        github_token = bot_token

    monkeypatch.setattr(vb, "get_user_config", lambda uid: _UC())
    monkeypatch.setattr(vb, "find_snapcraft_yaml", lambda client, owner, repo: found)
    monkeypatch.setattr(vb, "BotGitHubClient", lambda *a, **k: _FakeBotClient())


def test_coding_agent_async_task_leaves_bump_dispatched(isolated_session, monkeypatch):
    release_id = _seed(isolated_session)
    _patch_common(monkeypatch)
    monkeypatch.setattr(vb, "get_coding_dispatcher", lambda uc: object())

    class _Dispatcher:
        def start_task(self, owner, repo, prompt, base_ref="main", create_pull_request=True):
            return {"id": "task-123"}

    monkeypatch.setattr(vb, "get_coding_dispatcher", lambda uc: _Dispatcher())

    result = _agent(release_id)._run()

    assert "dispatched coding-agent task" in result
    with isolated_session() as session:
        bump = session.query(VersionBumpPR).one()
        assert bump.status == "dispatched"
        assert bump.external_task_id == "task-123"
        assert bump.bot_pr_url is None


def test_coding_agent_sync_task_opens_pr_immediately(isolated_session, monkeypatch):
    release_id = _seed(isolated_session)
    _patch_common(monkeypatch)

    class _Dispatcher:
        def start_task(self, owner, repo, prompt, base_ref="main", create_pull_request=True):
            return {"state": "completed", "html_url": "https://github.com/kenvandine/godot-snap/pull/42", "number": 42}

    monkeypatch.setattr(vb, "get_coding_dispatcher", lambda uc: _Dispatcher())

    result = _agent(release_id)._run()

    assert "opened PR" in result
    with isolated_session() as session:
        bump = session.query(VersionBumpPR).one()
        assert bump.status == "open"
        assert bump.bot_pr_number == 42
        assert bump.branch_name == "coding-agent/version-bump"
        release = session.query(UpstreamRelease).get(release_id)
        assert release.acted_on is True


def test_coding_agent_failure_creates_no_bump_row(isolated_session, monkeypatch):
    release_id = _seed(isolated_session)
    _patch_common(monkeypatch)

    class _Dispatcher:
        def start_task(self, owner, repo, prompt, base_ref="main", create_pull_request=True):
            return {"state": "failed", "error": "model produced no file changes"}

    monkeypatch.setattr(vb, "get_coding_dispatcher", lambda uc: _Dispatcher())

    result = _agent(release_id)._run()

    assert "failed" in result
    with isolated_session() as session:
        assert session.query(VersionBumpPR).count() == 0


def test_no_coding_backend_falls_back_to_regex_patch(isolated_session, monkeypatch):
    release_id = _seed(isolated_session)
    _patch_common(monkeypatch, found=("snap/snapcraft.yaml", "no matching source-tag here", "sha1"))
    monkeypatch.setattr(vb, "get_coding_dispatcher", lambda uc: None)

    result = _agent(release_id)._run()

    assert "no source-tag found to patch" in result
    assert "no coding backend configured" in result
    with isolated_session() as session:
        assert session.query(VersionBumpPR).count() == 0


def test_open_pr_already_exists_skips(isolated_session, monkeypatch):
    release_id = _seed(isolated_session)
    _patch_common(monkeypatch)
    with isolated_session() as session:
        session.add(
            VersionBumpPR(
                snap_id=1, upstream_release_id=release_id, packaging_repo="x",
                old_version="4.1", new_version="4.2", status="ci_pending",
            )
        )
        session.commit()

    called = False

    def _dispatcher(uc):
        nonlocal called
        called = True
        return object()

    monkeypatch.setattr(vb, "get_coding_dispatcher", _dispatcher)

    result = _agent(release_id)._run()

    assert "open version bump PR already exists" in result
    assert called is False


def test_refuses_to_bump_a_repo_not_owned_by_the_user(isolated_session, monkeypatch):
    """packaging_repo pointing at a third-party repo (misconfiguration) must
    never get an automatic version-bump PR opened against it."""
    with isolated_session() as session:
        session.add(User(id=1, github_login="kenvandine", github_id=1))
        snap = Snap(name="warble", packaging_repo="https://github.com/avojak/warble")
        session.add(snap)
        session.flush()
        release = UpstreamRelease(
            snap_id=snap.id, part_name="warble", latest_version="1.1", current_version="1.0",
        )
        session.add(release)
        session.commit()
        release_id = release.id

    _patch_common(monkeypatch)
    called = False

    def _dispatcher(uc):
        nonlocal called
        called = True
        return object()

    monkeypatch.setattr(vb, "get_coding_dispatcher", _dispatcher)

    result = _agent(
        release_id, snap_name="warble", packaging_repo="https://github.com/avojak/warble"
    )._run()

    assert "not owned by kenvandine" in result
    assert called is False
    with isolated_session() as session:
        assert session.query(VersionBumpPR).count() == 0
