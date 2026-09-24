"""Tests for colocated-vs-legacy test-repo resolution (orchestrator.resolve_test_repo)."""

from __future__ import annotations

from snap_dashboard.testing import orchestrator


def test_resolve_prefers_colocated_packaging_repo(monkeypatch):
    monkeypatch.setattr(orchestrator, "_path_exists_in_repo", lambda repo, path, token: True)

    repo, suite_path = orchestrator.resolve_test_repo(
        "kenvandine/some-snap", "some-snap", token="tok"
    )

    assert repo == "kenvandine/some-snap"
    assert suite_path == "tests/suite"


def test_resolve_returns_empty_when_not_colocated_and_no_fallback(monkeypatch):
    monkeypatch.setattr(orchestrator, "_path_exists_in_repo", lambda repo, path, token: False)

    repo, suite_path = orchestrator.resolve_test_repo(
        "kenvandine/some-snap", "some-snap", token="tok"
    )

    assert repo == ""
    assert suite_path == ""


def test_resolve_uses_explicit_fallback_when_configured(monkeypatch):
    monkeypatch.setattr(orchestrator, "_path_exists_in_repo", lambda repo, path, token: False)

    repo, suite_path = orchestrator.resolve_test_repo(
        None, "some-snap", token="tok", testing_repo_fallback="acme/custom-tests"
    )

    assert repo == "acme/custom-tests"
    assert suite_path == "suites/some-snap/suite"


def test_resolve_normalizes_full_url_packaging_repo(monkeypatch):
    # packaging_repo is stored in the DB as a full GitHub URL
    # (e.g. "https://github.com/kenvandine/geforcenow"), not a bare
    # "owner/repo" slug. resolve_test_repo() must normalize it before
    # calling _path_exists_in_repo(), which naively partitions on "/" and
    # would otherwise mis-parse the URL (owner="https:") and always report
    # no colocated suite, even when one exists.
    seen = {}

    def _fake_exists(repo, path, token):
        seen["repo"] = repo
        return True

    monkeypatch.setattr(orchestrator, "_path_exists_in_repo", _fake_exists)

    repo, suite_path = orchestrator.resolve_test_repo(
        "https://github.com/kenvandine/geforcenow", "geforcenow", token="tok"
    )

    assert seen["repo"] == "kenvandine/geforcenow"
    assert repo == "kenvandine/geforcenow"
    assert suite_path == "tests/suite"


def test_resolve_without_packaging_repo_or_fallback_returns_empty(monkeypatch):
    calls = []
    monkeypatch.setattr(
        orchestrator,
        "_path_exists_in_repo",
        lambda repo, path, token: calls.append(repo) or True,
    )

    repo, suite_path = orchestrator.resolve_test_repo(None, "some-snap", token="tok")

    assert calls == []  # never checked colocated path when there's no packaging repo
    assert repo == ""
    assert suite_path == ""


def test_suite_exists_in_repo_uses_resolved_repo(monkeypatch):
    seen = {}

    def _fake_exists(repo, path, token):
        seen["repo"] = repo
        seen["path"] = path
        return True

    monkeypatch.setattr(orchestrator, "_path_exists_in_repo", _fake_exists)

    assert orchestrator.suite_exists_in_repo(
        "", "some-snap", "tok", packaging_repo="kenvandine/some-snap"
    )
    assert seen["repo"] == "kenvandine/some-snap"
    assert seen["path"] == "tests/suite/__init__.robot"
