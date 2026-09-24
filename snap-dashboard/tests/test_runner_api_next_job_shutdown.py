"""Regression test for runner_api.py's next-job long-poll during shutdown.

uvicorn's ``timeout_graceful_shutdown`` (see cli.py / test_cli_serve_shutdown.py)
cancels any in-flight request task -- including this long-polling endpoint's
``asyncio.sleep()`` -- so `snap stop`/refresh isn't held up for the full
long-poll ``timeout``. Previously that CancelledError propagated out of the
handler unhandled, producing a scary "Exception in ASGI application"
traceback plus a 500 response logged on every single shutdown. It should
instead be treated the same as a normal long-poll timeout: a clean 204.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from snap_dashboard.web.routes import runner_api


@pytest.mark.anyio
async def test_next_job_returns_204_on_shutdown_cancellation():
    fake_runner = object()

    async def _cancel_sleep(*_args, **_kwargs):
        raise asyncio.CancelledError()

    with (
        patch.object(runner_api, "_auth_or_401", return_value=fake_runner),
        patch.object(runner_api, "_try_claim_job", return_value=None),
        patch.object(runner_api.asyncio, "sleep", _cancel_sleep),
    ):
        response = await runner_api.next_job(runner_id=2, timeout=25, authorization="Bearer x")

    assert response.status_code == 204


@pytest.mark.anyio
async def test_next_job_still_returns_job_when_claimed():
    fake_runner = object()
    fake_job = {"id": "job-1"}

    with (
        patch.object(runner_api, "_auth_or_401", return_value=fake_runner),
        patch.object(runner_api, "_try_claim_job", return_value=fake_job),
    ):
        response = await runner_api.next_job(runner_id=2, timeout=25, authorization="Bearer x")

    assert response.status_code == 200
