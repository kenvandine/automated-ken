"""Dependency checks/auto-install for a runner machine.

Called both from ``automated-ken-runner prepare-machine`` (manual,
verbose) and automatically at the start of the run loop (silent unless
something's missing), so already-enrolled runners self-heal the next
time their service restarts without an operator needing to remember to
run ``prepare-machine`` by hand.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Callable

logger = logging.getLogger(__name__)

# Snaps this runner needs installed (beyond the snap-under-test itself).
# Every job now runs the same generic desktop smoke test (see runner.py),
# so nothing beyond `snap`/`sudo` themselves is required here anymore —
# this list is intentionally empty (kept as a list, not removed, so a
# future per-repo requirement has an obvious place to go).
_REQUIRED_SNAPS: list[str] = []

# Other tools we can check for but won't try to auto-install, since
# there's no single reliable install method for them across distros
# (or, for `snap`/`loginctl`, they should always already be present on
# a system capable of running this snap at all).
_OTHER_REQUIRED_TOOLS = {
    "snap": "Required to install/refresh the snaps under test.",
    "loginctl": "Used for idle/lock detection (part of systemd, should always be present).",
    "xterm": (
        "Used to launch 'console app' snaps (Snap.is_console_app) inside a "
        "terminal window for the smoke test screenshot. Install with "
        "'sudo apt install xterm'."
    ),
}


def _snap_installed(name: str) -> bool:
    if not shutil.which("snap"):
        return False
    return (
        subprocess.run(
            ["snap", "list", name], capture_output=True, timeout=15, check=False
        ).returncode
        == 0
    )


def _install_snap(name: str) -> bool:
    result = subprocess.run(
        ["sudo", "snap", "install", name], capture_output=True, timeout=300, check=False
    )
    if result.returncode != 0:
        logger.warning(
            "auto-install of snap %r failed: %s", name, result.stderr.decode(errors="replace")
        )
    return result.returncode == 0


def ensure_dependencies(echo: Callable[[str], None] | None = None, auto_install: bool = True) -> bool:
    """Verify (and, for snap-installable tools, auto-install) prerequisites.

    Returns True if everything is satisfied by the end of this call,
    False otherwise. ``echo``, if given, is called with human-readable
    status lines (used by the ``prepare-machine`` CLI command); pass
    None for quiet operation with only logger output (used at run-loop
    startup, where we don't want a wall of text on every restart).
    """

    def _say(msg: str) -> None:
        if echo is not None:
            echo(msg)
        else:
            logger.info(msg)

    ok = True

    for cmd, why in _OTHER_REQUIRED_TOOLS.items():
        found = shutil.which(cmd)
        _say(f"  {cmd:12s} {found or 'NOT FOUND':30s} {why}")
        if not found:
            ok = False

    # Jobs run `sudo snap install/refresh` from a background service with
    # no terminal, so sudo must not prompt for a password for `snap`.
    if shutil.which("sudo") and shutil.which("snap"):
        nopasswd = subprocess.run(
            ["sudo", "-n", "snap", "version"], capture_output=True, timeout=15, check=False
        ).returncode == 0
        if nopasswd:
            _say(f"  {'sudo snap':12s} {'OK (no password prompt)':30s}")
        else:
            _say(
                f"  {'sudo snap':12s} {'NEEDS A PASSWORD':30s} Jobs can't install snaps. Allow it with a "
                "sudoers drop-in, e.g.: echo \"$USER ALL=(root) NOPASSWD: /usr/bin/snap\" | "
                "sudo tee /etc/sudoers.d/automated-ken-runner"
            )
            ok = False

    for snap_name in _REQUIRED_SNAPS:
        found = shutil.which(snap_name)
        if found:
            _say(f"  {snap_name:12s} {found:30s} (snap)")
            continue
        if _snap_installed(snap_name):
            # Installed but its /snap/bin symlink isn't on PATH yet (e.g.
            # right after enrollment, before a fresh login/service
            # restart picks up the new PATH) — nothing more to do here.
            _say(f"  {snap_name:12s} installed via snap but not yet on PATH")
            continue
        if not auto_install or not shutil.which("snap"):
            _say(f"  {snap_name:12s} NOT FOUND — run 'sudo snap install {snap_name}'")
            ok = False
            continue
        _say(f"  {snap_name:12s} NOT FOUND — installing via 'sudo snap install {snap_name}'...")
        if _install_snap(snap_name):
            _say(f"  {snap_name:12s} installed successfully.")
        else:
            _say(
                f"  {snap_name:12s} FAILED to auto-install — install manually with "
                f"'sudo snap install {snap_name}'."
            )
            ok = False

    return ok
