"""Tests for SnapcraftCredentialSyncAgent's token selection.

GitHub Actions secrets require admin-level access to the exact repo, and
forking never helps (secrets don't propagate across a fork). Since these
are always Ken's own packaging repos, his personal token — not the
separate bot account's token — is the one with sufficient access, so it
must be preferred here (unlike other agents' git-write paths, where the
bot token is preferred and the fork flow picks up the slack).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from snap_dashboard.agents import snapcraft_credential_sync as scs_module
from snap_dashboard.agents.snapcraft_credential_sync import SnapcraftCredentialSyncAgent


def _uc(**kwargs) -> SimpleNamespace:
    defaults = dict(snapcraft_macaroon="macaroon-value", bot_github_token="", github_token="")
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


@pytest.fixture
def capture_token(monkeypatch):
    calls: list[dict] = []

    def fake_sync(repos, credential_value, token):
        calls.append({"repos": repos, "credential_value": credential_value, "token": token})
        return [{"repo": r, "secret_names": ["SNAPCRAFT_STORE_CREDENTIALS"], "ok": True} for r in repos]

    monkeypatch.setattr(scs_module, "sync_snapcraft_credentials", fake_sync)
    return calls


def _run_agent(monkeypatch, uc, repos=("https://github.com/kenvandine/some-snap",)):
    monkeypatch.setattr(scs_module, "get_user_config", lambda user_id: uc)
    monkeypatch.setattr(
        scs_module,
        "get_session",
        lambda: _FakeSessionCtx(repos),
    )
    agent = SnapcraftCredentialSyncAgent(user_id=1)
    return agent._run()


class _FakeQuery:
    def __init__(self, repos):
        self._repos = repos

    def filter_by(self, **kwargs):
        return self

    def all(self):
        return [SimpleNamespace(packaging_repo=r) for r in self._repos]


class _FakeSession:
    def __init__(self, repos):
        self._repos = repos

    def query(self, model):
        return _FakeQuery(self._repos)


class _FakeSessionCtx:
    def __init__(self, repos):
        self._repos = repos

    def __enter__(self):
        return _FakeSession(self._repos)

    def __exit__(self, *exc):
        return False


def test_prefers_owner_personal_token_over_bot_token(monkeypatch, capture_token):
    uc = _uc(github_token="owner-pat", bot_github_token="bot-token")
    _run_agent(monkeypatch, uc)
    assert capture_token[0]["token"] == "owner-pat"


def test_falls_back_to_bot_token_when_no_personal_token(monkeypatch, capture_token):
    uc = _uc(github_token="", bot_github_token="bot-token")
    _run_agent(monkeypatch, uc)
    assert capture_token[0]["token"] == "bot-token"


def test_no_token_configured_is_skipped(monkeypatch, capture_token):
    uc = _uc(github_token="", bot_github_token="")
    result = _run_agent(monkeypatch, uc)
    assert result == "no GitHub token configured"
    assert capture_token == []
