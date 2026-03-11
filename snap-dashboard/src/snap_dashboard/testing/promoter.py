"""Snap promotion via ``snapcraft release``."""

from __future__ import annotations

import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)


def promote_snap(
    snap_name: str,
    revision: int,
    to_channel: str = "stable",
) -> tuple[bool, str]:
    """Run ``snapcraft release`` to promote *revision* to *to_channel*.

    Returns:
        A ``(success, output_or_error_message)`` tuple.  On success the combined
        stdout+stderr from snapcraft is returned as the message.
    """
    snapcraft = shutil.which("snapcraft")
    if not snapcraft:
        return False, "snapcraft not found in PATH"

    cmd = [snapcraft, "release", snap_name, str(revision), to_channel]
    logger.info("Running: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = result.stdout + result.stderr
        if result.returncode == 0:
            return True, output
        else:
            return False, output
    except subprocess.TimeoutExpired:
        return False, "snapcraft release timed out after 120s"
    except Exception as exc:
        return False, str(exc)


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
                        f"`{snap_name}` {version} has been promoted to `{testing_repo}`."
                        " Closing this PR."
                    )
                },
                headers=headers,
            )
            # Close the PR
            pr_url = f"{_GH_API}/repos/{owner}/{repo}/pulls/{pr_number}"
            client.patch(pr_url, json={"state": "closed"}, headers=headers)
    except Exception as exc:
        logger.warning("Failed to close test PR #%s: %s", pr_number, exc)
