"""Regression tests for per-snap rebuild dispatch inferring the right workflow.

Previously ``_dispatch_rebuild_for_snap`` only ever dispatched
``.github/workflows/automated-snap-build.yml`` — our own generated
workflow. Packaging repos that already have their own hand-written
build/publish workflow under a different filename (e.g. ``snap.yaml``)
silently no-op'd with a "no_workflow" status that was never surfaced
anywhere near the "Rebuild Now" button. These tests cover the fallback:
inspecting whatever dispatchable (``workflow_dispatch``) workflows the repo
actually has and picking the right one (directly when there's only one,
via a local model or heuristic when there's more than one), and that every
outcome — including skips — is recorded to ``StaleBuildTrigger`` so it's
visible in the UI.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from snap_dashboard.agents import stale_build_scanner as sbs
from snap_dashboard.db.models import Base, StaleBuildTrigger


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

    monkeypatch.setattr(sbs, "get_session", _fake_get_session)
    return session_local


class _FakeGitHubClient:
    """Stands in for BotGitHubClient — no real HTTP calls."""

    def __init__(self, workflow_files: dict[str, str], has_automated_workflow: bool = False):
        self._workflow_files = workflow_files
        self._has_automated_workflow = has_automated_workflow
        self.dispatched: list[tuple[str, str, str, dict | None]] = []
        self.dispatch_result: tuple[bool, str] = (True, "")

    def file_exists(self, owner, repo, path):
        return self._has_automated_workflow and path == sbs.WORKFLOW_PATH

    def list_tree(self, owner, repo, ref=None):
        return [f".github/workflows/{name}" for name in self._workflow_files]

    def get_file(self, owner, repo, path):
        name = path.rsplit("/", 1)[-1]
        content = self._workflow_files.get(name)
        return (content, "sha123") if content is not None else None

    def get_default_branch(self, owner, repo):
        return "main"

    def dispatch_workflow(self, owner, repo, workflow_file, ref="main", inputs=None):
        self.dispatched.append((owner, repo, workflow_file, inputs))
        return self.dispatch_result


_DISPATCHABLE = "on:\n  workflow_dispatch:\n"
_NOT_DISPATCHABLE = "on:\n  push:\n"


def _snap(snap_id=1, name="godot-4", packaging_repo="https://github.com/kenvandine/godot-snap"):
    return {"id": snap_id, "name": name, "packaging_repo": packaging_repo, "user_id": 1}


def test_single_dispatchable_workflow_used_directly(isolated_session):
    """Only one candidate workflow (like godot-snap's snap.yaml) — dispatch
    it directly, no model call needed."""
    client = _FakeGitHubClient({"snap.yaml": _DISPATCHABLE})
    status, err = sbs._dispatch_rebuild_for_snap(client, _snap())

    assert status == "triggered"
    assert err is None
    assert client.dispatched == [
        ("kenvandine", "godot-snap", "snap.yaml", None),
    ]

    with isolated_session() as session:
        trigger = session.query(StaleBuildTrigger).one()
        assert trigger.status == "triggered"
        assert trigger.workflow_file == "snap.yaml"


def test_no_dispatchable_workflow_is_skipped_and_recorded(isolated_session):
    """A repo with only push/schedule-triggered workflows can't be
    dispatched via the API at all — skip, but leave a visible record."""
    client = _FakeGitHubClient({"lint.yaml": _NOT_DISPATCHABLE})
    status, err = sbs._dispatch_rebuild_for_snap(client, _snap())

    assert status == "no_workflow"
    assert err is None
    assert client.dispatched == []

    with isolated_session() as session:
        trigger = session.query(StaleBuildTrigger).one()
        assert trigger.status == "skipped"
        assert trigger.workflow_file is None
        assert "no dispatchable" in trigger.error_msg


def test_own_generated_workflow_preferred_when_present(isolated_session):
    """When automated-snap-build.yml already exists, use it directly (fast
    path, no repo inspection/inference) and pass dashboard_trigger_id."""
    client = _FakeGitHubClient({"other.yaml": _DISPATCHABLE}, has_automated_workflow=True)
    status, _err = sbs._dispatch_rebuild_for_snap(client, _snap(snap_id=42))

    assert status == "triggered"
    assert client.dispatched == [
        ("kenvandine", "godot-snap", sbs._BUILD_WORKFLOW, {"dashboard_trigger_id": "42"}),
    ]


def test_multiple_candidates_uses_heuristic_when_no_model(isolated_session, monkeypatch):
    """Several dispatchable workflows and no local model available — fall
    back to a filename heuristic rather than failing outright."""

    class _NoClient:
        def is_available(self):
            return False

    import snap_dashboard.lemonade.client as lemonade_client_module
    monkeypatch.setattr(lemonade_client_module, "get_lemonade_client", lambda *a, **k: _NoClient())

    client = _FakeGitHubClient({
        "lint.yaml": _DISPATCHABLE,
        "publish-snap.yaml": _DISPATCHABLE,
    })
    status, _err = sbs._dispatch_rebuild_for_snap(client, _snap(), user_config=object())

    assert status == "triggered"
    assert client.dispatched[0][2] == "publish-snap.yaml"


def test_multiple_candidates_uses_model_choice(isolated_session, monkeypatch):
    """When a local model is available it picks between multiple candidates."""

    class _FakeModelClient:
        def is_available(self):
            return True

        def chat(self, prompt, temperature=0.1):
            return '{"workflow_file": "release.yaml"}'

    import snap_dashboard.lemonade.client as lemonade_client_module
    monkeypatch.setattr(lemonade_client_module, "get_lemonade_client", lambda *a, **k: _FakeModelClient())

    client = _FakeGitHubClient({
        "ci.yaml": _DISPATCHABLE,
        "release.yaml": _DISPATCHABLE,
    })
    status, _err = sbs._dispatch_rebuild_for_snap(client, _snap(), user_config=object())

    assert status == "triggered"
    assert client.dispatched[0][2] == "release.yaml"


def test_dispatch_failure_is_recorded_as_failed(isolated_session):
    client = _FakeGitHubClient({"snap.yaml": _DISPATCHABLE})
    client.dispatch_result = (False, "GitHub API returned 404")

    status, err = sbs._dispatch_rebuild_for_snap(client, _snap())

    assert status == "error"
    assert err == "GitHub API returned 404"
    with isolated_session() as session:
        trigger = session.query(StaleBuildTrigger).one()
        assert trigger.status == "failed"
        assert trigger.error_msg == "GitHub API returned 404"
