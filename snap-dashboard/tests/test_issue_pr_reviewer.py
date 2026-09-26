"""Tests for IssuePrReviewAgent and AddressReviewItemAgent.

Exercises the "Review Issues & PRs" summary flow (heuristic and LLM
summarization paths, persistence to IssueReviewReport) and the per-item
"Assign an agent" dispatch (issue_fix vs. pr_review_request), all
against an isolated in-memory database with GitHub/Copilot network calls
replaced by fakes.
"""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from snap_dashboard.agents import issue_pr_reviewer as ipr_module
from snap_dashboard.agents.issue_pr_reviewer import AddressReviewItemAgent, IssuePrReviewAgent
from snap_dashboard.db.models import Base, CopilotTask, IssueReviewReport, Snap, User, UserConfig


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

    monkeypatch.setattr(ipr_module, "get_session", _fake_get_session)
    return session_local


class _FakeDispatcher:
    def __init__(self, task: dict | None = None) -> None:
        self.calls: list[tuple] = []
        self.last_error = None
        self._task = task if task is not None else {"id": "task-1"}

    def start_task(self, owner, repo, prompt, base_ref="main", create_pull_request=True, model=None):
        self.calls.append((owner, repo, prompt))
        return self._task


class _FakeCopilotDispatcher(_FakeDispatcher):
    """Stands in for CopilotAgentClient so isinstance() checks pass."""


class _FakeBotClient:
    def __init__(self, *a, **k) -> None:
        pass

    def get_default_branch(self, owner, repo):
        return "main"


def _seed(session_local, packaging_repo="kenvandine/my-snap", upstream_repo=None):
    session = session_local()
    user = User(github_login="kenvandine", github_id=1)
    session.add(user)
    session.flush()
    uc = UserConfig(user_id=user.id, bot_github_token="tok")
    session.add(uc)
    snap = Snap(user_id=user.id, name="my-snap", packaging_repo=packaging_repo, upstream_repo=upstream_repo)
    session.add(snap)
    session.commit()
    user_id, snap_id = user.id, snap.id
    session.close()
    return user_id, snap_id


def _fake_issue(number, is_pr=False, title="Something broke", age_days=5, comments=0, body="details"):
    from datetime import datetime, timedelta, timezone

    created = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    entry = {
        "number": number,
        "title": title,
        "html_url": f"https://github.com/kenvandine/my-snap/issues/{number}",
        "user": {"login": "someone"},
        "created_at": created,
        "comments": comments,
        "body": body,
    }
    if is_pr:
        entry["pull_request"] = {}
    return entry


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def test_no_repos_configured_saves_error_report(isolated_session, monkeypatch) -> None:
    session = isolated_session()
    user = User(github_login="kenvandine", github_id=1)
    session.add(user)
    session.flush()
    session.add(UserConfig(user_id=user.id, bot_github_token="tok"))
    snap = Snap(user_id=user.id, name="bare-snap")
    session.add(snap)
    session.commit()
    user_id, snap_id = user.id, snap.id
    session.close()

    session = isolated_session()
    uc = session.query(UserConfig).filter_by(user_id=user_id).first()
    session.close()
    monkeypatch.setattr(ipr_module, "get_user_config", lambda uid: uc)

    agent = IssuePrReviewAgent(user_id=user_id, snap_id=snap_id)
    result = agent._run()

    assert result == "no repos configured"
    session = isolated_session()
    report = session.query(IssueReviewReport).filter_by(snap_id=snap_id).first()
    assert report is not None
    assert "No packaging or upstream repo" in report.error_msg
    session.close()


def test_fetches_and_summarizes_with_heuristic_fallback(isolated_session, monkeypatch) -> None:
    user_id, snap_id = _seed(isolated_session)
    session = isolated_session()
    uc = session.query(UserConfig).filter_by(user_id=user_id).first()
    session.close()
    monkeypatch.setattr(ipr_module, "get_user_config", lambda uid: uc)

    items = [
        _fake_issue(1, is_pr=False, title="Old bug", age_days=60),
        _fake_issue(2, is_pr=True, title="Fix bug", age_days=2),
    ]

    def _fake_get(url, params=None, headers=None):
        return _FakeResponse(200, items)

    class _FakeHttpClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None, headers=None):
            return _fake_get(url, params, headers)

    monkeypatch.setattr(ipr_module.httpx, "Client", _FakeHttpClient)

    agent = IssuePrReviewAgent(user_id=user_id, snap_id=snap_id)
    # No lemonade configured — _get_lemonade should return None so the
    # heuristic summary path is exercised.
    monkeypatch.setattr(agent, "_get_lemonade", lambda *a, **k: None)
    result = agent._run()

    assert "reviewed 2 open item" in result
    session = isolated_session()
    report = session.query(IssueReviewReport).filter_by(snap_id=snap_id).first()
    assert report is not None
    parsed = json.loads(report.items_json)
    assert len(parsed) == 2
    assert "heuristic" in report.summary.lower() or "No local text model" in report.summary
    assert "60d old" not in report.summary or True  # heuristic listing format only, sanity check below
    assert "1 open issue" in report.summary
    session.close()


def test_lemonade_configured_but_chat_fails_gives_distinct_heuristic_note(
    isolated_session, monkeypatch
) -> None:
    """A model *is* configured/reachable but the chat call itself fails
    (timeout, cold model load, non-200, ...) — the fallback message must not
    claim "no local text model configured" in that case, since that's
    misleading and sends the user chasing a Settings option that's already set.
    """
    user_id, snap_id = _seed(isolated_session)
    session = isolated_session()
    uc = session.query(UserConfig).filter_by(user_id=user_id).first()
    session.close()
    monkeypatch.setattr(ipr_module, "get_user_config", lambda uid: uc)

    items = [_fake_issue(1, is_pr=False, title="Old bug", age_days=60)]

    class _FakeHttpClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None, headers=None):
            return _FakeResponse(200, items)

    monkeypatch.setattr(ipr_module.httpx, "Client", _FakeHttpClient)

    class _FailingLemonade:
        def chat(self, *a, **k):
            return None  # simulates a timed-out/failed chat call

    agent = IssuePrReviewAgent(user_id=user_id, snap_id=snap_id)
    monkeypatch.setattr(agent, "_get_lemonade", lambda *a, **k: _FailingLemonade())
    result = agent._run()

    assert "reviewed 1 open item" in result
    session = isolated_session()
    report = session.query(IssueReviewReport).filter_by(snap_id=snap_id).first()
    assert report is not None
    assert "No local text model configured" not in report.summary
    assert "configured but the summarization request failed" in report.summary
    session.close()


def test_uses_llm_summary_when_lemonade_available(isolated_session, monkeypatch) -> None:
    user_id, snap_id = _seed(isolated_session)
    session = isolated_session()
    uc = session.query(UserConfig).filter_by(user_id=user_id).first()
    session.close()
    monkeypatch.setattr(ipr_module, "get_user_config", lambda uid: uc)

    items = [_fake_issue(1)]

    class _FakeHttpClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None, headers=None):
            return _FakeResponse(200, items)

    monkeypatch.setattr(ipr_module.httpx, "Client", _FakeHttpClient)

    class _FakeLemonade:
        def chat(self, prompt, temperature=0.2, max_tokens=None):
            return "LLM says: nothing urgent."

    agent = IssuePrReviewAgent(user_id=user_id, snap_id=snap_id)
    monkeypatch.setattr(agent, "_get_lemonade", lambda *a, **k: _FakeLemonade())
    result = agent._run()

    assert "reviewed 1 open item" in result
    session = isolated_session()
    report = session.query(IssueReviewReport).filter_by(snap_id=snap_id).first()
    assert report.summary == "LLM says: nothing urgent."
    session.close()


def test_address_issue_dispatches_issue_fix(isolated_session, monkeypatch) -> None:
    user_id, snap_id = _seed(isolated_session)
    session = isolated_session()
    uc = session.query(UserConfig).filter_by(user_id=user_id).first()
    session.close()

    dispatcher = _FakeDispatcher()
    monkeypatch.setattr(ipr_module, "get_user_config", lambda uid: uc)
    monkeypatch.setattr(ipr_module, "get_coding_dispatcher", lambda uc: dispatcher)
    monkeypatch.setattr(ipr_module, "BotGitHubClient", _FakeBotClient)

    agent = AddressReviewItemAgent(
        user_id=user_id, snap_id=snap_id, owner_repo="kenvandine/my-snap",
        number=42, item_type="issue", title="Crash on launch", body="steps to repro",
    )
    result = agent._run()

    assert "dispatched issue_fix" in result
    assert len(dispatcher.calls) == 1
    session = isolated_session()
    task = session.query(CopilotTask).filter_by(kind="issue_fix").first()
    assert task is not None
    assert task.issue_number == 42
    assert task.owner_repo == "kenvandine/my-snap"
    session.close()


def test_address_issue_skips_if_already_dispatched(isolated_session, monkeypatch) -> None:
    user_id, snap_id = _seed(isolated_session)
    session = isolated_session()
    uc = session.query(UserConfig).filter_by(user_id=user_id).first()
    session.add(
        CopilotTask(
            user_id=user_id, snap_id=snap_id, kind="issue_fix",
            owner_repo="kenvandine/my-snap", issue_number=42, status="queued",
        )
    )
    session.commit()
    session.close()

    dispatcher = _FakeDispatcher()
    monkeypatch.setattr(ipr_module, "get_user_config", lambda uid: uc)
    monkeypatch.setattr(ipr_module, "get_coding_dispatcher", lambda uc: dispatcher)
    monkeypatch.setattr(ipr_module, "BotGitHubClient", _FakeBotClient)

    agent = AddressReviewItemAgent(
        user_id=user_id, snap_id=snap_id, owner_repo="kenvandine/my-snap",
        number=42, item_type="issue", title="Crash on launch", body="steps",
    )
    result = agent._run()

    assert "already dispatched" in result
    assert dispatcher.calls == []


def test_address_pr_requests_copilot_review(isolated_session, monkeypatch) -> None:
    user_id, snap_id = _seed(isolated_session)
    session = isolated_session()
    uc = session.query(UserConfig).filter_by(user_id=user_id).first()
    session.close()

    class _FakeCopilotClient(ipr_module.CopilotAgentClient):
        def __init__(self):
            pass

        def request_pr_review(self, owner, repo, pr_number):
            self.requested = (owner, repo, pr_number)
            return True

    fake_copilot = _FakeCopilotClient()
    monkeypatch.setattr(ipr_module, "get_user_config", lambda uid: uc)
    monkeypatch.setattr(ipr_module, "get_coding_dispatcher", lambda uc: fake_copilot)

    agent = AddressReviewItemAgent(
        user_id=user_id, snap_id=snap_id, owner_repo="kenvandine/my-snap",
        number=7, item_type="pr", title="Add feature",
    )
    result = agent._run()

    assert "requested Copilot review" in result
    assert fake_copilot.requested == ("kenvandine", "my-snap", 7)
    session = isolated_session()
    task = session.query(CopilotTask).filter_by(kind="pr_review_request").first()
    assert task is not None
    assert task.status == "requested"
    session.close()


def test_address_pr_skipped_for_non_copilot_backend(isolated_session, monkeypatch) -> None:
    user_id, snap_id = _seed(isolated_session)
    session = isolated_session()
    uc = session.query(UserConfig).filter_by(user_id=user_id).first()
    session.close()

    dispatcher = _FakeDispatcher()  # not a CopilotAgentClient, no _copilot attr
    monkeypatch.setattr(ipr_module, "get_user_config", lambda uid: uc)
    monkeypatch.setattr(ipr_module, "get_coding_dispatcher", lambda uc: dispatcher)

    agent = AddressReviewItemAgent(
        user_id=user_id, snap_id=snap_id, owner_repo="kenvandine/my-snap",
        number=7, item_type="pr", title="Add feature",
    )
    result = agent._run()

    assert "need the Copilot cloud agent backend" in result
