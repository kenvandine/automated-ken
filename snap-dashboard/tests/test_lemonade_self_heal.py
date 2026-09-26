"""Tests for EmbeddedLemonadeManager's self-healing actions — reload_model()
and restart() — used by LemonadeClient's escalating recovery when repeated
requests against an already-running server keep failing (see
lemonade/client.py's ``_post_chat_completion``).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from snap_dashboard.lemonade import embedded as embedded_module
from snap_dashboard.lemonade.embedded import EmbeddedLemonadeManager


class _FakeResp:
    def __init__(self, status_code: int = 200, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _FakeHttpClient:
    def __init__(self, resp: _FakeResp) -> None:
        self._resp = resp

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json, headers):
        return self._resp


def _manager() -> EmbeddedLemonadeManager:
    return EmbeddedLemonadeManager()


def test_reload_model_returns_false_if_not_running():
    manager = _manager()
    manager._proc = None
    assert manager.reload_model("some-model") is False


def test_reload_model_returns_true_on_success():
    manager = _manager()
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None  # running
    manager._proc = fake_proc

    with patch("httpx.Client", return_value=_FakeHttpClient(_FakeResp(200))):
        assert manager.reload_model("some-model", ctx_size=8192) is True


def test_reload_model_returns_false_on_http_error_status():
    manager = _manager()
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None
    manager._proc = fake_proc

    with patch("httpx.Client", return_value=_FakeHttpClient(_FakeResp(500, "boom"))):
        assert manager.reload_model("some-model") is False


def test_reload_model_returns_false_on_connection_error():
    manager = _manager()
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None
    manager._proc = fake_proc

    class _RaisingClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json, headers):
            import httpx

            raise httpx.ConnectError("refused")

    with patch("httpx.Client", return_value=_RaisingClient()):
        assert manager.reload_model("some-model") is False


def test_restart_stops_and_relaunches():
    manager = _manager()
    manager._last_restart_at = 0.0  # long ago -> cooldown does not block
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None
    manager._proc = fake_proc
    manager._pulled_models = {"already-loaded-model"}

    with (
        patch.object(manager, "stop") as fake_stop,
        patch.object(manager, "ensure_started", return_value=True) as fake_ensure,
    ):
        result = manager.restart(reason="test")

    assert result is True
    fake_stop.assert_called_once()
    fake_ensure.assert_called_once()
    # A fresh process has nothing loaded -- bookkeeping must be cleared so
    # the next ensure_started() warm-up re-issues /v1/load for everything.
    assert manager._pulled_models == set()


def test_restart_respects_cooldown_and_skips_actual_restart():
    manager = _manager()
    manager._last_restart_at = __import__("time").monotonic()  # just restarted
    manager._proc = None

    with (
        patch.object(manager, "stop") as fake_stop,
        patch.object(manager, "ensure_started") as fake_ensure,
    ):
        result = manager.restart(reason="test")

    assert result is False  # not running, and restart was skipped
    fake_stop.assert_not_called()
    fake_ensure.assert_not_called()


def test_restart_cooldown_constant_is_reasonable():
    # Sanity guard against an accidental typo turning this into something
    # tiny (defeats the anti-thrashing purpose) or absurdly large (a real
    # transient failure would then never get a second restart attempt).
    assert 30 <= embedded_module._RESTART_COOLDOWN_SECONDS <= 600
