"""Tests for ``testing.promoter.promote_snap`` -- Store API based promotion.

Promotion talks to the Snap Store's authenticated publisher API in-process
via ``craft-store`` instead of shelling out to the (unavailable, in this
strictly-confined snap) ``snapcraft`` CLI. See ``testing/promoter.py``.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from craft_store import errors as store_errors

from snap_dashboard.testing.promoter import promote_snap


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


def test_promote_snap_requires_credentials() -> None:
    ok, message = promote_snap("evince", 1065, "stable", store_credentials="")
    assert ok is False
    assert "credentials" in message.lower()


@patch("snap_dashboard.testing.promoter.craft_store.UbuntuOneStoreClient")
def test_promote_snap_success(mock_client_cls: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client.request.return_value = _FakeResponse({"opened_channels": ["stable"]})
    mock_client_cls.return_value = mock_client

    ok, message = promote_snap(
        "evince", 1065, "stable", store_credentials="fake-macaroon-blob"
    )

    assert ok is True
    assert "evince" in message
    assert "stable" in message

    # Verify the request matches what ``snapcraft release`` itself sends.
    mock_client.request.assert_called_once()
    args, kwargs = mock_client.request.call_args
    assert args[0] == "POST"
    assert args[1] == "https://dashboard.snapcraft.io/dev/api/snap-release/"
    assert kwargs["json"] == {
        "name": "evince",
        "revision": "1065",
        "channels": ["stable"],
    }

    # No login flow: client constructed with ephemeral + environment_auth,
    # not an interactive login.
    _, ctor_kwargs = mock_client_cls.call_args
    assert ctor_kwargs["ephemeral"] is True
    assert ctor_kwargs["environment_auth"] == "SNAPCRAFT_STORE_CREDENTIALS"


@patch("snap_dashboard.testing.promoter.craft_store.UbuntuOneStoreClient")
def test_promote_snap_store_error(mock_client_cls: MagicMock) -> None:
    mock_client = MagicMock()
    fake_response = MagicMock()
    fake_response.status_code = 401
    fake_response.reason = "Unauthorized"

    def _raise(*_args, **_kwargs):
        raise store_errors.StoreServerError(fake_response)

    mock_client.request.side_effect = _raise
    mock_client_cls.return_value = mock_client

    ok, message = promote_snap(
        "evince", 1065, "stable", store_credentials="stale-macaroon"
    )

    assert ok is False
    assert message


@patch("snap_dashboard.testing.promoter.craft_store.UbuntuOneStoreClient")
def test_promote_snap_restores_env_var(mock_client_cls: MagicMock) -> None:
    """The env var used to smuggle credentials into craft_store.Auth must not
    leak into the process environment after the call returns.
    """
    mock_client = MagicMock()
    mock_client.request.return_value = _FakeResponse({})
    mock_client_cls.return_value = mock_client

    os.environ.pop("SNAPCRAFT_STORE_CREDENTIALS", None)
    promote_snap("evince", 1065, "stable", store_credentials="fake-macaroon-blob")
    assert "SNAPCRAFT_STORE_CREDENTIALS" not in os.environ


@patch("snap_dashboard.testing.promoter.craft_store.UbuntuOneStoreClient")
def test_promote_snap_restores_previous_env_var(mock_client_cls: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client.request.return_value = _FakeResponse({})
    mock_client_cls.return_value = mock_client

    os.environ["SNAPCRAFT_STORE_CREDENTIALS"] = "original-value"
    try:
        promote_snap("evince", 1065, "stable", store_credentials="fake-macaroon-blob")
        assert os.environ["SNAPCRAFT_STORE_CREDENTIALS"] == "original-value"
    finally:
        os.environ.pop("SNAPCRAFT_STORE_CREDENTIALS", None)
