"""Tests for RepoNormalizerAgent.

Covers the idempotency checks (AGENTS.md already present, or a
fleet_normalize CopilotTask already dispatched), the suite-inlining helper,
and the end-to-end dispatch flow (packaging-repo task + testing-repo cleanup
task) with GitHub/Copilot network calls replaced by fakes.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from snap_dashboard.agents import repo_normalizer as rn_module
from snap_dashboard.agents.repo_normalizer import RepoNormalizerAgent
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

    monkeypatch.setattr(rn_module, "get_session", _fake_get_session)
    return session_local


class _FakeDispatcher:
    def __init__(self, task: dict | None = None) -> None:
        self.calls: list[tuple] = []
        self._task = task if task is not None else {"id": "task-1"}

    def start_task(self, owner, repo, prompt, base_ref="main", create_pull_request=True, model=None):
        self.calls.append((owner, repo, prompt))
        return self._task


class _FakeBotClient:
    def __init__(self, existing_agents_md: set[str] | None = None) -> None:
        self.existing = existing_agents_md or set()

    def file_exists(self, owner, repo, path):
        return f"{owner}/{repo}" in self.existing


def _seed(session_local, packaging_repo="kenvandine/my-snap", testing_repo="kenvandine/automated-ken-tests"):
    session = session_local()
    user = User(github_login="kenvandine", github_id=1)
    session.add(user)
    session.flush()
    uc = UserConfig(
        user_id=user.id,
        fleet_normalization_enabled=True,
        bot_github_token="tok",
        testing_repo=testing_repo,
    )
    session.add(uc)
    snap = Snap(user_id=user.id, name="my-snap", packaging_repo=packaging_repo)
    session.add(snap)
    session.commit()
    user_id, snap_id = user.id, snap.id
    session.close()
    return user_id, snap_id


def test_build_suite_block_no_testing_repo() -> None:
    block, moved = RepoNormalizerAgent._build_suite_block("", "my-snap", "tok")
    assert block == ""
    assert moved is False


def test_build_suite_block_includes_file_contents(monkeypatch) -> None:
    monkeypatch.setattr(
        rn_module, "list_suite_files", lambda repo, snap, token: {"suite/test.yaml": b"steps: []"}
    )
    block, moved = RepoNormalizerAgent._build_suite_block("kenvandine/automated-ken-tests", "my-snap", "tok")
    assert moved is True
    assert "tests/suite/test.yaml" in block
    assert "steps: []" in block


def test_build_suite_block_truncates_when_too_large(monkeypatch) -> None:
    big_files = {f"suite/file{i}.yaml": b"x" * 5000 for i in range(20)}
    monkeypatch.setattr(rn_module, "list_suite_files", lambda repo, snap, token: big_files)
    block, moved = RepoNormalizerAgent._build_suite_block("kenvandine/automated-ken-tests", "my-snap", "tok")
    assert moved is True
    assert "omitted for length" in block
    assert len(block) < sum(len(v) for v in big_files.values())


def test_build_suite_block_skips_binary_files(monkeypatch) -> None:
    monkeypatch.setattr(
        rn_module, "list_suite_files", lambda repo, snap, token: {"suite/logo.png": b"\xff\xd8\xff\x00"}
    )
    block, moved = RepoNormalizerAgent._build_suite_block("kenvandine/automated-ken-tests", "my-snap", "tok")
    assert moved is True
    assert "binary file" in block
    assert "skipped" in block


def test_normalize_repo_skips_if_agents_md_exists(isolated_session, monkeypatch) -> None:
    user_id, snap_id = _seed(isolated_session)
    monkeypatch.setattr(rn_module, "list_suite_files", lambda *a, **k: None)
    bot_client = _FakeBotClient(existing_agents_md={"kenvandine/my-snap"})
    dispatcher = _FakeDispatcher()

    agent = RepoNormalizerAgent(user_id=user_id)
    did = agent._normalize_repo(
        bot_client, dispatcher, snap_id, "my-snap", "kenvandine/my-snap",
        "kenvandine/automated-ken-tests", "tok",
    )

    assert did is False
    assert dispatcher.calls == []


def test_normalize_repo_dispatches_and_records_task(isolated_session, monkeypatch) -> None:
    user_id, snap_id = _seed(isolated_session)
    monkeypatch.setattr(rn_module, "list_suite_files", lambda *a, **k: None)
    bot_client = _FakeBotClient()
    dispatcher = _FakeDispatcher()

    agent = RepoNormalizerAgent(user_id=user_id)
    did = agent._normalize_repo(
        bot_client, dispatcher, snap_id, "my-snap", "kenvandine/my-snap",
        "kenvandine/automated-ken-tests", "tok",
    )

    assert did is True
    assert len(dispatcher.calls) == 1
    owner, repo, prompt = dispatcher.calls[0]
    assert (owner, repo) == ("kenvandine", "my-snap")
    assert "sync-release" in prompt
    assert "AGENTS.md" in prompt

    session = isolated_session()
    tasks = session.query(CopilotTask).filter_by(kind="fleet_normalize").all()
    assert len(tasks) == 1
    assert tasks[0].owner_repo == "kenvandine/my-snap"
    assert tasks[0].status == "queued"
    session.close()


def test_normalize_repo_skips_if_task_already_dispatched(isolated_session, monkeypatch) -> None:
    user_id, snap_id = _seed(isolated_session)
    monkeypatch.setattr(rn_module, "list_suite_files", lambda *a, **k: None)
    session = isolated_session()
    session.add(
        CopilotTask(
            user_id=user_id, snap_id=snap_id, kind="fleet_normalize",
            owner_repo="kenvandine/my-snap", status="queued",
        )
    )
    session.commit()
    session.close()

    bot_client = _FakeBotClient()
    dispatcher = _FakeDispatcher()
    agent = RepoNormalizerAgent(user_id=user_id)
    did = agent._normalize_repo(
        bot_client, dispatcher, snap_id, "my-snap", "kenvandine/my-snap",
        "kenvandine/automated-ken-tests", "tok",
    )

    assert did is False
    assert dispatcher.calls == []


def test_normalize_repo_also_dispatches_suite_cleanup(isolated_session, monkeypatch) -> None:
    user_id, snap_id = _seed(isolated_session)
    monkeypatch.setattr(
        rn_module, "list_suite_files", lambda *a, **k: {"suite/test.yaml": b"steps: []"}
    )
    bot_client = _FakeBotClient()
    dispatcher = _FakeDispatcher()

    agent = RepoNormalizerAgent(user_id=user_id)
    agent._normalize_repo(
        bot_client, dispatcher, snap_id, "my-snap", "kenvandine/my-snap",
        "kenvandine/automated-ken-tests", "tok",
    )

    # One task against the packaging repo, one cleanup task against the testing repo.
    assert len(dispatcher.calls) == 2
    repos_targeted = {(c[0], c[1]) for c in dispatcher.calls}
    assert ("kenvandine", "my-snap") in repos_targeted
    assert ("kenvandine", "automated-ken-tests") in repos_targeted

    session = isolated_session()
    tasks = session.query(CopilotTask).filter_by(kind="fleet_normalize").all()
    assert len(tasks) == 2
    session.close()


def test_run_disabled_returns_early(isolated_session, monkeypatch) -> None:
    user_id, _ = _seed(isolated_session)
    session = isolated_session()
    uc = session.query(UserConfig).filter_by(user_id=user_id).first()
    uc.fleet_normalization_enabled = False
    session.commit()
    session.close()

    monkeypatch.setattr(rn_module, "get_user_config", lambda uid: uc)
    agent = RepoNormalizerAgent(user_id=user_id)
    assert agent._run() == "disabled"
