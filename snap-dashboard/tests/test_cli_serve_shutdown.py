"""Regression test for cli.py's serve() command bounding uvicorn's
graceful-shutdown wait, so `snap stop`/refresh doesn't hang on in-flight
long-poll connections (see web/routes/runner_api.py's next-job long-poll)
or a slow embedded-model subprocess.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from snap_dashboard.cli import serve


def test_serve_bounds_graceful_shutdown_timeout():
    fake_uvicorn = MagicMock()
    with (
        patch.dict("sys.modules", {"uvicorn": fake_uvicorn}),
        patch("snap_dashboard.cli.get_config") as get_config,
    ):
        get_config.return_value = MagicMock(port=9080, bind="127.0.0.1")
        CliRunner().invoke(serve, [])

    fake_uvicorn.run.assert_called_once()
    _args, kwargs = fake_uvicorn.run.call_args
    assert kwargs.get("timeout_graceful_shutdown") == 10
