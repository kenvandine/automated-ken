"""Tests for reasoning-model ("thinking") handling in LemonadeClient.

Our opinionated text/coding models (see lemonade/models.py) are Qwen3
reasoning models. On the raw OpenAI-compat /v1/chat/completions endpoint,
these emit a hidden `<think>...</think>` block *before* any final answer
unless the request explicitly disables it via
``chat_template_kwargs.enable_thinking``. A modest ``max_tokens`` budget
(as most of our callers use) can get entirely consumed by that hidden
reasoning, leaving the OpenAI-compat ``content`` field empty even on an
otherwise-successful (200 OK) response — see agents/issue_pr_reviewer.py's
"Lemonade summarization returned nothing" warning.
"""

from __future__ import annotations

from unittest.mock import patch

from snap_dashboard.lemonade.client import LemonadeClient


class _FakeResp:
    def __init__(self, json_data: dict, status_code: int = 200) -> None:
        self._json = json_data
        self.status_code = status_code
        self.text = "ok"

    def json(self) -> dict:
        return self._json


class _OneShotClient:
    """Fake httpx.Client that captures the outgoing payload and returns one response."""

    def __init__(self, resp: _FakeResp) -> None:
        self._resp = resp
        self.calls = 0
        self.last_payload: dict | None = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json, headers):
        self.calls += 1
        self.last_payload = json
        return self._resp


def _client() -> LemonadeClient:
    return LemonadeClient(base_url="http://localhost:13305", model="test-model")


def test_chat_disables_thinking_by_default():
    fake = _OneShotClient(
        _FakeResp({"choices": [{"message": {"content": "hello"}}], "usage": {}})
    )
    with (
        patch("httpx.Client", return_value=fake),
        patch("snap_dashboard.lemonade.client.record_model_usage"),
    ):
        result = _client().chat("hi")
    assert result == "hello"
    assert fake.last_payload["chat_template_kwargs"] == {"enable_thinking": False}


def test_chat_can_opt_into_thinking():
    fake = _OneShotClient(
        _FakeResp({"choices": [{"message": {"content": "hello"}}], "usage": {}})
    )
    with (
        patch("httpx.Client", return_value=fake),
        patch("snap_dashboard.lemonade.client.record_model_usage"),
    ):
        result = _client().chat("hi", enable_thinking=True)
    assert result == "hello"
    assert fake.last_payload["chat_template_kwargs"] == {"enable_thinking": True}


def test_chat_falls_back_to_reasoning_content_when_content_empty():
    """If a model ignores enable_thinking=False (or thinking still leaks
    through), an empty ``content`` alongside a non-empty
    ``reasoning_content`` should surface the reasoning text rather than
    being treated as if the model never replied.
    """
    fake = _OneShotClient(
        _FakeResp(
            {
                "choices": [
                    {"message": {"content": "", "reasoning_content": "the answer is 42"}}
                ],
                "usage": {},
            }
        )
    )
    with (
        patch("httpx.Client", return_value=fake),
        patch("snap_dashboard.lemonade.client.record_model_usage"),
    ):
        result = _client().chat("hi")
    assert result == "the answer is 42"


def test_chat_returns_none_when_both_content_and_reasoning_are_empty():
    fake = _OneShotClient(
        _FakeResp({"choices": [{"message": {"content": "", "reasoning_content": ""}}], "usage": {}})
    )
    with (
        patch("httpx.Client", return_value=fake),
        patch("snap_dashboard.lemonade.client.record_model_usage") as record_usage,
    ):
        result = _client().chat("hi")
    assert result is None
    record_usage.assert_not_called()


def test_vision_compare_disables_thinking():
    fake = _OneShotClient(
        _FakeResp({"choices": [{"message": {"content": '{"decision": "approve", "confidence": 1}'}}], "usage": {}})
    )
    with (
        patch("httpx.Client", return_value=fake),
        patch("snap_dashboard.lemonade.client.record_model_usage"),
    ):
        result = _client().vision_compare(b"a", b"b", "snap", "1.0", "2.0")
    assert result == {"decision": "approve", "confidence": 1, "reasoning": ""}
    assert fake.last_payload["chat_template_kwargs"] == {"enable_thinking": False}


def test_vision_inspect_disables_thinking():
    fake = _OneShotClient(
        _FakeResp({"choices": [{"message": {"content": '{"decision": "approve", "confidence": 1}'}}], "usage": {}})
    )
    with (
        patch("httpx.Client", return_value=fake),
        patch("snap_dashboard.lemonade.client.record_model_usage"),
    ):
        result = _client().vision_inspect(b"a", "snap", "1.0")
    assert result == {"decision": "approve", "confidence": 0.35, "reasoning": ""}
    assert fake.last_payload["chat_template_kwargs"] == {"enable_thinking": False}
