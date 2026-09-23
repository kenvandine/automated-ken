"""Tests for UpstreamMaintainerAgent.

Exercises the "is this repo owned by the logged-in user" heuristic and the
end-to-end ``_run()`` flow (dep-update dispatch, issue-fix dispatch capped
per run, dedup via CopilotTask) against an isolated in-memory database, with
GitHub/Copilot network calls replaced by fakes.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from snap_dashboard.agents import upstream_maintainer as um_module
from snap_dashboard.agents.upstream_maintainer import UpstreamMaintainerAgent
from snap_dashboard.db.models import Base, CopilotTask, Snap, User, UserConfig


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

    monkeypatch.setattr(um_module, "get_session", _fake_get_session)
    return session_local


class _FakeDispatcher:
    """Records start_task calls and returns a fixed task dict."""

    def __init__(self, task: dict | None = None) -> None:
        self.calls: list[tuple] = []
        self._task = task if task is not None else {"id": "task-1"}

    def start_task(self, owner, repo, prompt, base_ref="main", create_pull_request=True, model=None):
        self.calls.append((owner, repo, prompt))
        return self._task


def _seed_user_and_snap(session_local, upstream_repo: str, login: str = "kenvandine"):
    session = session_local()
    user = User(github_login=login, github_id=12345)
    session.add(user)
    session.flush()
    uc = UserConfig(user_id=user.id, auto_maintain_upstream=True, bot_github_token="tok")
    session.add(uc)
    snap = Snap(user_id=user.id, name="gemini-desktop", upstream_repo=upstream_repo)
    session.add(snap)
    session.commit()
    user_id = user.id
    session.close()
    return user_id


def test_owned_by_matches_case_insensitively() -> None:
    assert UpstreamMaintainerAgent._owned_by("https://github.com/KenVandine/gemini-desktop", "kenvandine")


def test_owned_by_false_for_other_owner() -> None:
    assert not UpstreamMaintainerAgent._owned_by("https://github.com/someoneelse/thing", "kenvandine")


def test_owned_by_false_without_login() -> None:
    assert not UpstreamMaintainerAgent._owned_by("https://github.com/kenvandine/thing", "")


def test_run_skips_when_disabled(isolated_session, monkeypatch) -> None:
    user_id = _seed_user_and_snap(isolated_session, "https://github.com/kenvandine/gemini-desktop")
    session = isolated_session()
    uc = session.query(UserConfig).filter_by(user_id=user_id).first()
    uc.auto_maintain_upstream = False
    session.commit()
    session.close()

    monkeypatch.setattr(um_module, "get_user_config", lambda uid: uc)
    agent = UpstreamMaintainerAgent(user_id=user_id)
    assert agent._run() == "disabled"


def test_run_dispatches_dep_update_for_owned_repo(isolated_session, monkeypatch) -> None:
    user_id = _seed_user_and_snap(isolated_session, "https://github.com/kenvandine/gemini-desktop")
    session = isolated_session()
    uc = session.query(UserConfig).filter_by(user_id=user_id).first()
    session.close()

    dispatcher = _FakeDispatcher()
    monkeypatch.setattr(um_module, "get_user_config", lambda uid: uc)
    monkeypatch.setattr(um_module, "get_coding_dispatcher", lambda uc_: dispatcher)
    monkeypatch.setattr(
        UpstreamMaintainerAgent, "_maybe_request_reviews", lambda self, *a, **k: False
    )
    monkeypatch.setattr(
        UpstreamMaintainerAgent, "_maybe_triage_issues", lambda self, *a, **k: False
    )

    agent = UpstreamMaintainerAgent(user_id=user_id)
    summary = agent._run()

    assert "1 upstream repo" in summary
    assert len(dispatcher.calls) == 1
    owner, repo, prompt = dispatcher.calls[0]
    assert (owner, repo) == ("kenvandine", "gemini-desktop")
    assert "outdated npm/node dependencies" in prompt

    session = isolated_session()
    tasks = session.query(CopilotTask).filter_by(kind="dep_update").all()
    assert len(tasks) == 1
    assert tasks[0].status == "queued"
    session.close()


def test_dep_update_respects_cooldown(isolated_session, monkeypatch) -> None:
    user_id = _seed_user_and_snap(isolated_session, "https://github.com/kenvandine/gemini-desktop")
    session = isolated_session()
    uc = session.query(UserConfig).filter_by(user_id=user_id).first()
    snap = session.query(Snap).filter_by(user_id=user_id).first()
    # A dep_update task dispatched yesterday should block a new one today.
    session.add(
        CopilotTask(
            user_id=user_id,
            snap_id=snap.id,
            kind="dep_update",
            owner_repo="kenvandine/gemini-desktop",
            status="completed",
            created_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
    )
    session.commit()
    session.close()

    dispatcher = _FakeDispatcher()
    monkeypatch.setattr(um_module, "get_user_config", lambda uid: uc)
    monkeypatch.setattr(um_module, "get_coding_dispatcher", lambda uc_: dispatcher)
    monkeypatch.setattr(
        UpstreamMaintainerAgent, "_maybe_request_reviews", lambda self, *a, **k: False
    )
    monkeypatch.setattr(
        UpstreamMaintainerAgent, "_maybe_triage_issues", lambda self, *a, **k: False
    )

    agent = UpstreamMaintainerAgent(user_id=user_id)
    agent._run()

    assert len(dispatcher.calls) == 0  # cooldown should have blocked it


def test_triage_issues_caps_per_run_and_dedupes(isolated_session, monkeypatch) -> None:
    user_id = _seed_user_and_snap(isolated_session, "https://github.com/kenvandine/gemini-desktop")
    session = isolated_session()
    uc = session.query(UserConfig).filter_by(user_id=user_id).first()
    snap = session.query(Snap).filter_by(user_id=user_id).first()
    snap_id = snap.id
    session.close()

    dispatcher = _FakeDispatcher()

    class _FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class _FakeHttpClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None, headers=None):
            if url.endswith("/issues"):
                return _FakeResponse([{"number": n, "title": f"issue {n}", "body": ""} for n in range(1, 6)])
            return _FakeResponse([])

    monkeypatch.setattr(um_module.httpx, "Client", _FakeHttpClient)

    agent = UpstreamMaintainerAgent(user_id=user_id)
    acted = agent._maybe_triage_issues(dispatcher, snap_id, "kenvandine", "gemini-desktop", "tok")

    assert acted is True
    assert len(dispatcher.calls) == 3  # _MAX_ISSUES_PER_RUN

    # Running again shouldn't re-dispatch the same 3 issues, only the remaining 2.
    dispatcher.calls.clear()
    agent._maybe_triage_issues(dispatcher, snap_id, "kenvandine", "gemini-desktop", "tok")
    assert len(dispatcher.calls) == 2
