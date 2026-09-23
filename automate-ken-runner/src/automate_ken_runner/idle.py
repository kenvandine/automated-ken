"""Idle/lock detection for the current graphical session.

Uses ``loginctl`` (systemd-logind) as the primary, desktop-environment-agnostic
signal — works on GNOME, KDE, or anything using logind. Falls back to the
GNOME Mutter ``IdleMonitor`` D-Bus interface for a more precise idle-seconds
figure when available; degrades gracefully (returns ``idle_seconds=None``)
when neither is available rather than raising, since idle detection must
never crash the runner loop.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class IdleState:
    locked: bool
    idle_seconds: int | None  # None when it could not be determined


def _current_session_id() -> str | None:
    """Return the current logind session ID, e.g. from $XDG_SESSION_ID."""
    sid = os.environ.get("XDG_SESSION_ID")
    if sid:
        return sid
    try:
        out = subprocess.run(
            ["loginctl", "show-user", str(os.getuid()), "-p", "Display", "--value"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        display_session = out.stdout.strip()
        return display_session or None
    except (OSError, subprocess.SubprocessError):
        return None


def _loginctl_property(session_id: str, prop: str) -> str | None:
    try:
        out = subprocess.run(
            ["loginctl", "show-session", session_id, "-p", prop, "--value"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if out.returncode != 0:
            return None
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _mutter_idle_ms() -> int | None:
    """Query GNOME Mutter's IdleMonitor D-Bus interface for precise idle time."""
    try:
        out = subprocess.run(
            [
                "gdbus", "call", "--session",
                "--dest", "org.gnome.Mutter.IdleMonitor",
                "--object-path", "/org/gnome/Mutter/IdleMonitor/Core",
                "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime",
            ],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if out.returncode != 0:
            return None
        # Output looks like "(uint64 12345,)"
        digits = "".join(ch for ch in out.stdout if ch.isdigit())
        return int(digits) if digits else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def get_idle_state() -> IdleState:
    """Best-effort determination of whether this desktop session is idle/locked."""
    session_id = _current_session_id()
    locked = False
    idle_seconds: int | None = None

    if session_id:
        locked_hint = _loginctl_property(session_id, "LockedHint")
        if locked_hint is not None:
            locked = locked_hint.lower() == "yes"
        idle_hint = _loginctl_property(session_id, "IdleHint")
        if idle_hint is not None and idle_hint.lower() == "no":
            idle_seconds = 0

    mutter_ms = _mutter_idle_ms()
    if mutter_ms is not None:
        idle_seconds = mutter_ms // 1000

    return IdleState(locked=locked, idle_seconds=idle_seconds)


def is_safe_to_claim_job(idle_threshold_seconds: int = 120) -> bool:
    """Return True only when we're confident nobody is actively using this machine.

    Deliberately conservative: an *unknown* idle state (couldn't be
    determined at all) is treated as "not safe" rather than assuming the
    machine is free — never steal a session out from under a real person.
    """
    state = get_idle_state()
    if state.locked:
        return False
    if state.idle_seconds is None:
        return False
    return state.idle_seconds >= idle_threshold_seconds
