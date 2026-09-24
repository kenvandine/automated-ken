"""Tests for EmbeddedLemonadeManager.stop()'s shortened terminate/kill
timeout, and web/app.py's on_shutdown hard-exiting the process afterwards
(see the comment there — bypasses ThreadPoolExecutor's atexit thread-join
so a long-running background agent can't make `snap stop`/refresh hang).
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

from snap_dashboard.lemonade.embedded import EmbeddedLemonadeManager


def test_stop_terminates_and_waits_with_short_timeout():
    manager = EmbeddedLemonadeManager()
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None  # still running
    manager._proc = fake_proc

    manager.stop()

    fake_proc.terminate.assert_called_once()
    fake_proc.wait.assert_called_once_with(timeout=5)
    fake_proc.kill.assert_not_called()


def test_stop_kills_if_terminate_does_not_finish_in_time():
    manager = EmbeddedLemonadeManager()
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None
    fake_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="lemond", timeout=5)
    manager._proc = fake_proc

    manager.stop()

    fake_proc.terminate.assert_called_once()
    fake_proc.kill.assert_called_once()


def test_stop_is_a_no_op_when_no_process_running():
    manager = EmbeddedLemonadeManager()
    manager._proc = None
    manager.stop()  # must not raise


def test_on_shutdown_stops_lemonade_then_hard_exits():
    import asyncio

    from snap_dashboard.web import app as app_module

    fake_manager = MagicMock()
    with (
        patch(
            "snap_dashboard.lemonade.embedded.get_embedded_manager",
            return_value=fake_manager,
        ),
        patch("os._exit") as fake_exit,
    ):
        asyncio.run(app_module.on_shutdown())

    fake_manager.stop.assert_called_once()
    fake_exit.assert_called_once_with(0)
