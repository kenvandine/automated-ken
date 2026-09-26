"""Tests for LemonadeClient.vision_inspect() — single-screenshot judging
when no baseline exists yet (see agents/test_run_auto_promoter.py).
"""

from __future__ import annotations

import json
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


def _resp_with_decision(decision: str, confidence: float, reasoning: str = "ok") -> _FakeResp:
    content = json.dumps({"decision": decision, "confidence": confidence, "reasoning": reasoning})
    return _FakeResp({"choices": [{"message": {"content": content}}]})


def test_vision_inspect_returns_decision_shape():
    resp = _resp_with_decision("approve", 0.2, "A window with the app UI is visible.")
    with (
        patch("httpx.Client", return_value=_FakeClient(resp)),
        patch("snap_dashboard.lemonade.client.record_model_usage"),
    ):
        result = _client().vision_inspect(
            image_bytes=b"fakepng", snap_name="evince", version="45.0"
        )
    assert result is not None
    assert result["decision"] == "approve"
    assert "window" in result["reasoning"]


def test_vision_inspect_caps_confidence_even_if_model_overshoots():
    # The model might ignore the "keep it low" instruction and return 0.95 —
    # vision_inspect() must clamp this, since a single screenshot with no
    # baseline is a much weaker signal than a real comparison.
    resp = _resp_with_decision("approve", 0.95, "Looks great.")
    with (
        patch("httpx.Client", return_value=_FakeClient(resp)),
        patch("snap_dashboard.lemonade.client.record_model_usage"),
    ):
        result = _client().vision_inspect(
            image_bytes=b"fakepng", snap_name="evince", version="45.0"
        )
    assert result is not None
    assert result["confidence"] <= 0.35


def test_vision_inspect_returns_none_on_http_error():
    resp = _FakeResp({}, status_code=500)
    # 500 is treated as a possibly-transient "still cold-loading" error and
    # retried a couple of times before giving up — patch out the real sleep
    # between attempts so this test stays fast.
    with (
        patch("httpx.Client", return_value=_FakeClient(resp)),
        patch("snap_dashboard.lemonade.client.time.sleep"),
    ):
        result = _client().vision_inspect(
            image_bytes=b"fakepng", snap_name="evince", version="45.0"
        )
    assert result is None
