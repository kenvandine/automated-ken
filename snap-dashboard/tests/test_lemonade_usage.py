"""Tests for LemonadeClient token-usage recording (real usage vs. estimate).

See lemonade/client.py's ``_record_usage()`` helper, called from ``chat()``
and ``vision_compare()`` after every successful request.
"""

from __future__ import annotations

from unittest.mock import patch

from snap_dashboard.lemonade.client import LemonadeClient


class _FakeResp:
    def __init__(self, json_data: dict, status_code: int = 200) -> None:
        self._json = json_data
        self.status_code = status_code
        self.text = "error"

    def json(self) -> dict:
        return self._json


class _FakeClient:
    def __init__(self, resp: _FakeResp) -> None:
        self._resp = resp

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json, headers):
        return self._resp

    def get(self, url, headers):
        return self._resp


def _client() -> LemonadeClient:
    return LemonadeClient(base_url="http://localhost:13305", model="test-model")


def test_chat_uses_real_usage_from_api_when_present():
    resp = _FakeResp(
        {
            "choices": [{"message": {"content": "hello there"}}],
            "usage": {"prompt_tokens": 42, "completion_tokens": 7},
        }
    )
    with patch("httpx.Client", return_value=_FakeClient(resp)), \
            patch("snap_dashboard.lemonade.client.record_model_usage") as record:
        reply = _client().chat("hi")
    assert reply == "hello there"
    record.assert_called_once_with(
        provider="lemonade",
        model="test-model",
        task="chat",
        input_tokens=42,
        output_tokens=7,
        estimated=False,
    )


def test_chat_falls_back_to_estimate_when_usage_missing():
    resp = _FakeResp({"choices": [{"message": {"content": "a" * 40}}]})
    with patch("httpx.Client", return_value=_FakeClient(resp)), \
            patch("snap_dashboard.lemonade.client.record_model_usage") as record:
        _client().chat("b" * 20)
    kwargs = record.call_args.kwargs
    assert kwargs["estimated"] is True
    assert kwargs["output_tokens"] == 10  # 40 chars / 4
    assert kwargs["input_tokens"] > 0


def test_vision_compare_adds_per_image_estimate_when_usage_missing():
    resp = _FakeResp(
        {"choices": [{"message": {"content": '{"decision": "approve", "confidence": 0.9, "reasoning": "ok"}'}}]}
    )
    with patch("httpx.Client", return_value=_FakeClient(resp)), \
            patch("snap_dashboard.lemonade.client.record_model_usage") as record:
        result = _client().vision_compare(b"base", b"new", "geforcenow", "1.0", "2.0")
    assert result["decision"] == "approve"
    kwargs = record.call_args.kwargs
    assert kwargs["task"] == "vision_compare"
    assert kwargs["estimated"] is True
    # Two images worth of estimated tokens (800 each) plus prompt text.
    assert kwargs["input_tokens"] >= 1600


def test_vision_compare_uses_real_usage_when_present():
    resp = _FakeResp(
        {
            "choices": [{"message": {"content": '{"decision": "reject", "confidence": 0.8, "reasoning": "bad"}'}}],
            "usage": {"prompt_tokens": 1234, "completion_tokens": 56},
        }
    )
    with patch("httpx.Client", return_value=_FakeClient(resp)), \
            patch("snap_dashboard.lemonade.client.record_model_usage") as record:
        _client().vision_compare(b"base", b"new", "geforcenow", "1.0", "2.0")
    record.assert_called_once_with(
        provider="lemonade",
        model="test-model",
        task="vision_compare",
        input_tokens=1234,
        output_tokens=56,
        estimated=False,
    )


def test_failed_request_does_not_record_usage():
    resp = _FakeResp({}, status_code=500)
    # 500 is retried a couple of times before giving up — patch out the real
    # sleep between attempts so this test stays fast.
    with patch("httpx.Client", return_value=_FakeClient(resp)), \
            patch("snap_dashboard.lemonade.client.record_model_usage") as record, \
            patch("snap_dashboard.lemonade.client.time.sleep"):
        result = _client().chat("hi")
    assert result is None
    record.assert_not_called()
