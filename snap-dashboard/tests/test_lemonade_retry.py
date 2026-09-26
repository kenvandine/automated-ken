"""Tests for LemonadeClient's retry-with-backoff around /v1/chat/completions.

See lemonade/client.py's ``_post_chat_completion`` — most first-request
failures against the embedded server are "still cold-loading/pulling this
model" rather than a real, permanent error, so retryable statuses (and
connection-level errors) get a couple of short-backoff retries instead of
immediately falling back to heuristics.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx

from snap_dashboard.lemonade.client import _MAX_ATTEMPTS, LemonadeClient


class _FakeResp:
    def __init__(self, json_data: dict, status_code: int = 200) -> None:
        self._json = json_data
        self.status_code = status_code
        self.text = "error"

    def json(self) -> dict:
        return self._json


def _reply_resp(text: str = "ok") -> _FakeResp:
    return _FakeResp(
        {"choices": [{"message": {"content": text}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    )


class _ScriptedClient:
    """Fake httpx.Client whose .post() returns/raises a scripted sequence."""

    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json, headers):
        self.calls += 1
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _client() -> LemonadeClient:
    return LemonadeClient(base_url="http://localhost:13305", model="test-model")


def test_retries_retryable_status_then_succeeds():
    scripted = _ScriptedClient([_FakeResp({}, status_code=503), _reply_resp("hello")])
    with (
        patch("httpx.Client", return_value=scripted),
        patch("snap_dashboard.lemonade.client.record_model_usage"),
        patch("snap_dashboard.lemonade.client.time.sleep") as sleep,
    ):
        result = _client().chat("hi")
    assert result == "hello"
    assert scripted.calls == 2
    sleep.assert_called_once()  # one backoff between attempt 1 and attempt 2


def test_does_not_retry_non_retryable_status():
    scripted = _ScriptedClient([_FakeResp({}, status_code=400)])
    with (
        patch("httpx.Client", return_value=scripted),
        patch("snap_dashboard.lemonade.client.time.sleep") as sleep,
    ):
        result = _client().chat("hi")
    assert result is None
    assert scripted.calls == 1  # no retry for a real client error
    sleep.assert_not_called()


def test_gives_up_after_max_attempts_on_persistent_retryable_status():
    scripted = _ScriptedClient([_FakeResp({}, status_code=503)] * _MAX_ATTEMPTS)
    with (
        patch("httpx.Client", return_value=scripted),
        patch("snap_dashboard.lemonade.client.time.sleep"),
    ):
        result = _client().chat("hi")
    assert result is None
    assert scripted.calls == _MAX_ATTEMPTS


def test_retries_on_connection_error_then_succeeds():
    scripted = _ScriptedClient([httpx.ConnectError("refused"), _reply_resp("hi there")])
    with (
        patch("httpx.Client", return_value=scripted),
        patch("snap_dashboard.lemonade.client.record_model_usage"),
        patch("snap_dashboard.lemonade.client.time.sleep"),
    ):
        result = _client().chat("hi")
    assert result == "hi there"
    assert scripted.calls == 2


def test_json_decode_error_treated_as_parse_failure_not_retried():
    class _BadJsonResp(_FakeResp):
        def json(self):
            raise json.JSONDecodeError("bad", "doc", 0)

    scripted = _ScriptedClient([_BadJsonResp({})])
    with (
        patch("httpx.Client", return_value=scripted),
        patch("snap_dashboard.lemonade.client.time.sleep") as sleep,
    ):
        result = _client().chat("hi")
    assert result is None
    assert scripted.calls == 1
    sleep.assert_not_called()
