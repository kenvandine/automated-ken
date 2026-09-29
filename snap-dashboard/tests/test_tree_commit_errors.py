"""Tests that GitTreeClient surfaces the actual GitHub error on failure.

Regression coverage for a real incident: the local Lemonade coding backend
committed a genuinely non-empty plan, but writing it to the bot's fork hit a
403 on the git/blobs endpoint. The only thing that made it into the
CopilotTask/UI error message was "failed to commit changes to a new
branch" — no status code, no response body — so diagnosing a token/scope
problem required digging through raw server logs instead of the dashboard.
``last_error`` (and ``_describe_http_error``) exist so that detail survives.
"""

from __future__ import annotations

import functools

import httpx
import pytest

from snap_dashboard.github.tree_commit import GitTreeClient


@pytest.fixture
def mock_transport(monkeypatch):
    """Patch ``httpx.Client`` (as imported in tree_commit) to serve a fixed
    happy-path sequence except for whichever request the test overrides."""
    responses: dict[str, httpx.Response] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.method} {request.url.path}"
        if key in responses:
            return responses[key]
        if request.method == "GET" and request.url.path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "abc123"}})
        if request.method == "GET" and request.url.path.endswith("/git/commits/abc123"):
            return httpx.Response(200, json={"tree": {"sha": "tree123"}})
        if request.method == "GET" and request.url.path.count("/") == 2:
            return httpx.Response(200, json={"default_branch": "main"})
        if request.method == "POST" and request.url.path.endswith("/git/blobs"):
            return httpx.Response(201, json={"sha": "blob123"})
        if request.method == "POST" and request.url.path.endswith("/git/trees"):
            return httpx.Response(201, json={"sha": "newtree123"})
        if request.method == "POST" and request.url.path.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": "newcommit123"})
        if request.method == "GET" and request.url.path.endswith("/heads/my-branch"):
            return httpx.Response(404)
        if request.method == "POST" and request.url.path.endswith("/git/refs"):
            return httpx.Response(201, json={})
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    import snap_dashboard.github.tree_commit as tree_commit_module

    monkeypatch.setattr(
        tree_commit_module.httpx, "Client", functools.partial(httpx.Client, transport=transport),
    )
    return responses


def test_commit_multi_records_status_and_body_on_blob_write_403(mock_transport) -> None:
    mock_transport["POST /repos/o/r/git/blobs"] = httpx.Response(
        403, json={"message": "Resource not accessible by personal access token"},
    )
    client = GitTreeClient(token="bot-token")

    result = client.commit_multi("o", "r", "my-branch", "msg", put_files={"f.txt": "content"})

    assert result is None
    assert client.last_error is not None
    assert "403" in client.last_error
    assert "Resource not accessible" in client.last_error
    assert "git/blobs" in client.last_error


def test_create_pr_records_status_and_body_on_403(mock_transport) -> None:
    mock_transport["POST /repos/o/r/pulls"] = httpx.Response(
        403, json={"message": "Resource not accessible by personal access token"},
    )
    client = GitTreeClient(token="bot-token")

    result = client.create_pr("o", "r", title="t", body="b", head="my-branch", base="main")

    assert result is None
    assert client.last_error is not None
    assert "403" in client.last_error
    assert "Resource not accessible" in client.last_error


def test_commit_multi_succeeds_and_leaves_no_error(mock_transport) -> None:
    client = GitTreeClient(token="bot-token")

    result = client.commit_multi("o", "r", "my-branch", "msg", put_files={"f.txt": "content"})

    assert result == "my-branch"
    assert client.last_error is None
