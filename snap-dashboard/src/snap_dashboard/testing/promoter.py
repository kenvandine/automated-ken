"""Snap promotion via the authenticated Snap Store publisher API.

This dashboard runs as a strictly-confined snap, which means the
``snapcraft`` CLI binary is never on ``PATH`` -- even on hosts that
happen to have it installed as their own snap, strict confinement
prevents this app from invoking it. Shelling out is also the wrong
model in general: it would either fail outright or fall back to an
interactive login flow, when we already hold a valid exported Store
credential (``UserConfig.snapcraft_macaroon``, produced by
``snapcraft export-login``).

Instead we talk to the Store's authenticated publisher API directly,
in-process, via ``craft-store`` -- the same library ``snapcraft``
itself uses. The endpoint/auth wiring here (``UbuntuOneStoreClient``,
the ``dashboard.snapcraft.io`` legacy ``/dev/api/snap-release/``
endpoint, and the ``SNAPCRAFT_STORE_CREDENTIALS`` environment
variable) mirrors exactly what ``snapcraft release`` does internally
(see ``snapcraft/store/client.py``'s ``StoreClientCLI.release``), so
existing exported macaroons work unchanged.
"""

from __future__ import annotations

import logging
import os
import threading

import craft_store
from craft_store import endpoints as store_endpoints
from craft_store import errors as store_errors

from snap_dashboard.github.utils import parse_owner_repo

logger = logging.getLogger(__name__)

_STORE_URL = "https://dashboard.snapcraft.io"
_STORE_UPLOAD_URL = "https://storage.snapcraftcontent.com"
_UBUNTU_ONE_SSO_URL = "https://login.ubuntu.com"
_ENVIRONMENT_STORE_CREDENTIALS = "SNAPCRAFT_STORE_CREDENTIALS"
_USER_AGENT = "automated-ken-snap-dashboard/1.0"

# ``craft_store.Auth`` reads the credential out of the environment variable
# once, synchronously, inside the client constructor. Promotions can run
# concurrently for different snaps/users in the agent runner's thread pool,
# so this lock serializes the "set env var -> construct client -> clear env
# var" critical section to avoid one promotion's credential leaking into
# another's request.
_ENV_LOCK = threading.Lock()


def _build_store_client(store_credentials: str) -> craft_store.UbuntuOneStoreClient:
    """Construct a Store client authenticated with *store_credentials*."""
    previous = os.environ.get(_ENVIRONMENT_STORE_CREDENTIALS)
    os.environ[_ENVIRONMENT_STORE_CREDENTIALS] = store_credentials
    try:
        return craft_store.UbuntuOneStoreClient(
            base_url=_STORE_URL,
            storage_base_url=_STORE_UPLOAD_URL,
            auth_url=_UBUNTU_ONE_SSO_URL,
            application_name="automated-ken",
            user_agent=_USER_AGENT,
            endpoints=store_endpoints.U1_SNAP_STORE,
            environment_auth=_ENVIRONMENT_STORE_CREDENTIALS,
            ephemeral=True,
        )
    finally:
        if previous is None:
            os.environ.pop(_ENVIRONMENT_STORE_CREDENTIALS, None)
        else:
            os.environ[_ENVIRONMENT_STORE_CREDENTIALS] = previous


def promote_snap(
    snap_name: str,
    revision: int,
    to_channel: str = "stable",
    store_credentials: str = "",
) -> tuple[bool, str]:
    """Release *revision* of *snap_name* to *to_channel* via the Store API.

    Args:
        store_credentials: A ``snapcraft export-login`` credential
            (``UserConfig.snapcraft_macaroon``). Required -- there's no
            ambient/CLI login to fall back to.

    Returns:
        A ``(success, output_or_error_message)`` tuple.
    """
    if not store_credentials:
        return (
            False,
            "No Snap Store credentials configured. Add one on the Settings page.",
        )

    try:
        with _ENV_LOCK:
            client = _build_store_client(store_credentials)
            response = client.request(
                "POST",
                f"{_STORE_URL}/dev/api/snap-release/",
                json={
                    "name": snap_name,
                    "revision": str(revision),
                    "channels": [to_channel],
                },
            )
    except store_errors.CraftStoreError as exc:
        logger.warning("Store release failed for %s: %s", snap_name, exc)
        return False, str(exc)
    except Exception as exc:
        logger.warning("Store release failed for %s: %s", snap_name, exc)
        return False, str(exc)

    try:
        data = response.json()
    except ValueError:
        return True, response.text

    channels = data.get("channel_map_tree", {}) or data.get("opened_channels", data)
    return True, f"Released {snap_name} revision {revision} to {to_channel}: {channels}"


def close_test_pr(
    testing_repo: str,
    pr_number: int,
    snap_name: str,
    version: str,
    token: str,
) -> None:
    """Post a comment on the test PR and close it after a successful promotion.

    Failures are logged at WARNING level rather than raised, as the promotion
    itself has already succeeded.
    """
    if not token or not testing_repo:
        return
    owner, _, repo = testing_repo.partition("/")
    if not repo:
        return

    import httpx

    _GH_API = "https://api.github.com"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }

    try:
        with httpx.Client(timeout=30) as client:
            # Post a closing comment
            comment_url = f"{_GH_API}/repos/{owner}/{repo}/issues/{pr_number}/comments"
            client.post(
                comment_url,
                json={
                    "body": (
                        f"`{snap_name}` {version} has been promoted to `stable`. "
                        "Closing this PR."
                    )
                },
                headers=headers,
            )
            # Close the PR
            pr_url = f"{_GH_API}/repos/{owner}/{repo}/pulls/{pr_number}"
            client.patch(pr_url, json={"state": "closed"}, headers=headers)
    except Exception as exc:
        logger.warning("Failed to close test PR #%s: %s", pr_number, exc)


def merge_packaging_pr(packaging_repo: str, pr_number: int, token: str) -> bool:
    """Merge a packaging PR using the maintainer's token."""
    if not token or not packaging_repo or not pr_number:
        return False

    owner_repo = parse_owner_repo(packaging_repo)
    if not owner_repo:
        return False
    owner, repo = owner_repo

    import httpx

    _GH_API = "https://api.github.com"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.put(
                f"{_GH_API}/repos/{owner}/{repo}/pulls/{pr_number}/merge",
                json={"merge_method": "squash"},
                headers=headers,
            )
        return resp.status_code in (200, 201)
    except Exception as exc:
        logger.warning("Failed to merge packaging PR #%s: %s", pr_number, exc)
        return False
