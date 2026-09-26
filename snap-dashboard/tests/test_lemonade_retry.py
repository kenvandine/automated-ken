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


# ----------------------------------------------------------------------
# Self-healing escalation: once a full retry cycle is exhausted, heal_
# callbacks (e.g. reload the model, then restart lemond) run in order,
# each followed by one more full retry cycle, before finally giving up.
# ----------------------------------------------------------------------


def _client_with_heals(heal_callbacks) -> LemonadeClient:
    client = LemonadeClient(base_url="http://localhost:13305", model="test-model")
    client.heal_callbacks = heal_callbacks
    return client


def test_succeeds_after_first_heal_step():
    # Exhausts the initial cycle (all 503s), then the request succeeds
    # right after the first heal step runs.
    scripted = _ScriptedClient(
        [_FakeResp({}, status_code=503)] * _MAX_ATTEMPTS + [_reply_resp("recovered")]
    )
    reload_calls = []
    restart_calls = []
    client = _client_with_heals(
        [
            ("reload_model", lambda: (reload_calls.append(1), True)[1]),
            ("restart_lemond", lambda: (restart_calls.append(1), True)[1]),
        ]
    )
    with (
        patch("httpx.Client", return_value=scripted),
        patch("snap_dashboard.lemonade.client.record_model_usage"),
        patch("snap_dashboard.lemonade.client.time.sleep"),
    ):
        result = client.chat("hi")
    assert result == "recovered"
    assert len(reload_calls) == 1
    assert len(restart_calls) == 0  # never needed to escalate further


def test_escalates_to_second_heal_step_if_first_does_not_fix_it():
    # First cycle exhausted, heal step 1 (reload) runs but the retried
    # cycle still fails, heal step 2 (restart) runs and then it succeeds.
    scripted = _ScriptedClient(
        [_FakeResp({}, status_code=503)] * _MAX_ATTEMPTS  # initial cycle
        + [_FakeResp({}, status_code=503)] * _MAX_ATTEMPTS  # after reload
        + [_reply_resp("recovered after restart")]  # after restart
    )
    calls = []
    client = _client_with_heals(
        [
            ("reload_model", lambda: (calls.append("reload"), True)[1]),
            ("restart_lemond", lambda: (calls.append("restart"), True)[1]),
        ]
    )
    with (
        patch("httpx.Client", return_value=scripted),
        patch("snap_dashboard.lemonade.client.record_model_usage"),
        patch("snap_dashboard.lemonade.client.time.sleep"),
    ):
        result = client.chat("hi")
    assert result == "recovered after restart"
    assert calls == ["reload", "restart"]


def test_gives_up_after_all_heal_steps_exhausted():
    scripted = _ScriptedClient([_FakeResp({}, status_code=503)] * (_MAX_ATTEMPTS * 3))
    calls = []
    client = _client_with_heals(
        [
            ("reload_model", lambda: (calls.append("reload"), False)[1]),
            ("restart_lemond", lambda: (calls.append("restart"), False)[1]),
        ]
    )
    with (
        patch("httpx.Client", return_value=scripted),
        patch("snap_dashboard.lemonade.client.time.sleep"),
    ):
        result = client.chat("hi")
    assert result is None
    assert calls == ["reload", "restart"]
    assert scripted.calls == _MAX_ATTEMPTS * 3


def test_non_retryable_status_skips_self_heal_entirely():
    # A 400 is a real client-side error -- no amount of reloading/
    # restarting lemond fixes a malformed request, so heal steps must
    # never even be attempted.
    scripted = _ScriptedClient([_FakeResp({}, status_code=400)])
    calls = []
    client = _client_with_heals([("reload_model", lambda: (calls.append("reload"), True)[1])])
    with (
        patch("httpx.Client", return_value=scripted),
        patch("snap_dashboard.lemonade.client.time.sleep"),
    ):
        result = client.chat("hi")
    assert result is None
    assert calls == []
    assert scripted.calls == 1


def test_heal_step_exception_does_not_crash_the_caller():
    scripted = _ScriptedClient(
        [_FakeResp({}, status_code=503)] * _MAX_ATTEMPTS + [_reply_resp("still ok")]
    )

    def _broken_heal():
        raise RuntimeError("manager exploded")

    client = _client_with_heals([("reload_model", _broken_heal)])
    with (
        patch("httpx.Client", return_value=scripted),
        patch("snap_dashboard.lemonade.client.record_model_usage"),
        patch("snap_dashboard.lemonade.client.time.sleep"),
    ):
        result = client.chat("hi")
    assert result == "still ok"  # heal step raising still allows the retry to proceed

