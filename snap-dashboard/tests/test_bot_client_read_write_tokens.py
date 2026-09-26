"""Tests that read-only GitHub API calls use ``read_token`` (the repo
owner's own token, when supplied) while writes stay on ``token`` (the bot
account) — see ``BotGitHubClient``/``GitTreeClient``'s ``read_token`` param.

Managing a repo the bot isn't a full collaborator on (e.g. GitHub Actions
secrets, which need admin access) only works at all if reads can fall back
to a token that reliably already has access — the repo owner's own PAT.
Writes must still go through the bot token so commits/PRs are attributed to
the bot account, not the human owner.
"""

from __future__ import annotations

import functools

import httpx
import pytest

from snap_dashboard.github.bot_client import BotGitHubClient
from snap_dashboard.github.tree_commit import GitTreeClient

OWNER_TOKEN = "owner-pat"
BOT_TOKEN = "bot-token"


@pytest.fixture
def recorded_requests(monkeypatch):
    """Patch ``httpx.Client`` (as imported in both client modules) so every
    request is served by a mock transport and its Authorization header is
    recorded, keyed by HTTP method."""
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.headers.get("authorization", "")))
        if request.method == "GET" and request.url.path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "abc123"}})
        if request.method == "GET" and request.url.path.endswith("/git/commits/abc123"):
            return httpx.Response(200, json={"tree": {"sha": "tree123"}})
        if request.method == "GET" and "/repos/" in request.url.path and request.url.path.count("/") == 2:
            return httpx.Response(200, json={"default_branch": "main"})
        if request.method == "POST" and request.url.path.endswith("/git/blobs"):
            return httpx.Response(201, json={"sha": "blob123"})
        if request.method == "POST" and request.url.path.endswith("/git/trees"):
            return httpx.Response(201, json={"sha": "newtree123"})
        if request.method == "POST" and request.url.path.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": "newcommit123"})
        if request.method == "GET" and request.url.path.endswith("/git/ref/heads/my-branch"):
            return httpx.Response(404)
        if request.method == "POST" and request.url.path.endswith("/git/refs"):
            return httpx.Response(201, json={})
        return httpx.Response(200, json={})

    mock_transport = httpx.MockTransport(handler)
    real_client_cls = httpx.Client
    make_client = functools.partial(real_client_cls, transport=mock_transport)

    import snap_dashboard.github.bot_client as bot_client_module
    import snap_dashboard.github.tree_commit as tree_commit_module

    monkeypatch.setattr(bot_client_module.httpx, "Client", make_client)
    monkeypatch.setattr(tree_commit_module.httpx, "Client", make_client)
    return calls


def _auths(calls: list[tuple[str, str]], method: str) -> set[str]:
    return {auth for m, auth in calls if m == method}


def test_bot_client_reads_use_read_token(recorded_requests):
    client = BotGitHubClient(BOT_TOKEN, read_token=OWNER_TOKEN)
    assert client.get_default_branch("kenvandine", "some-snap") == "main"
    gets = _auths(recorded_requests, "GET")
    assert gets == {f"Bearer {OWNER_TOKEN}"}


def test_bot_client_writes_use_bot_token(recorded_requests):
    client = BotGitHubClient(BOT_TOKEN, read_token=OWNER_TOKEN)
    assert client.create_branch("kenvandine", "some-snap", "my-branch", "abc123") is True
    posts = _auths(recorded_requests, "POST")
    assert posts == {f"Bearer {BOT_TOKEN}"}


def test_bot_client_defaults_read_token_to_token_when_unset(recorded_requests):
    client = BotGitHubClient(BOT_TOKEN)
    client.get_default_branch("kenvandine", "some-snap")
    gets = _auths(recorded_requests, "GET")
    assert gets == {f"Bearer {BOT_TOKEN}"}


def test_tree_commit_reads_base_state_with_read_token_writes_with_bot_token(recorded_requests):
    client = GitTreeClient(BOT_TOKEN, read_token=OWNER_TOKEN)
    result = client.commit_multi(
        "kenvandine", "some-snap", "my-branch", "msg", put_files={"a.txt": "hi"}
    )
    assert result == "my-branch"
    gets = _auths(recorded_requests, "GET")
    posts = _auths(recorded_requests, "POST")
    # Base state (default branch, ref, commit) is read from the real repo
    # using the owner's token...
    assert f"Bearer {OWNER_TOKEN}" in gets
    # ...but the final branch-existence check happens against push_owner
    # (which may be the bot's own fork the owner token can't see at all),
    # so it correctly still uses the bot token.
    assert f"Bearer {BOT_TOKEN}" in gets
    assert posts == {f"Bearer {BOT_TOKEN}"}
