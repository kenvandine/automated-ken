"""One-time desktop-session setup for a newly-onboarded runner machine.

A runner needs an always-on, always-unlocked graphical session to test
GUI snaps non-interactively: autologin (so it comes back up ready after
any reboot/power loss with nobody at the keyboard), no screen lock, no
screen blanking/suspend (which would black out the very screen we need
to screenshot), and the screenshot-capture GNOME Shell extension (see
``docs/gnome-screenshot-extension/``) installed and loaded.

Called from ``automated-ken-runner prepare-machine`` alongside
``deps.ensure_dependencies`` so onboarding a machine is a single command.
Every step here is idempotent — re-running this on an already-configured
machine is a no-op (returns quickly, does not reboot). Unlike the
dependency check, the run loop does not call this on startup.
"""

from __future__ import annotations

import getpass
import logging
import shutil
import subprocess
from importlib import resources
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

_EXTENSION_UUID = "automated-ken-screenshot@kenvandine.github.io"
_GDM_CONFIG_CANDIDATES = [Path("/etc/gdm3/custom.conf"), Path("/etc/gdm/custom.conf")]

# gsettings knobs that would otherwise blank/lock/suspend the screen out
# from under a running (or about-to-run) test. Applied unconditionally —
# each `gsettings set` call is itself idempotent.
_SCREENSAVER_SETTINGS = [
    ("org.gnome.desktop.screensaver", "lock-enabled", "false"),
    ("org.gnome.desktop.screensaver", "idle-activation-enabled", "false"),
    ("org.gnome.desktop.session", "idle-delay", "uint32 0"),
    ("org.gnome.settings-daemon.plugins.power", "idle-dim", "false"),
    ("org.gnome.settings-daemon.plugins.power", "sleep-inactive-ac-type", "'nothing'"),
    ("org.gnome.settings-daemon.plugins.power", "sleep-inactive-battery-type", "'nothing'"),
    ("org.gnome.desktop.lockdown", "disable-lock-screen", "true"),
]


def _say(echo: Callable[[str], None] | None, msg: str) -> None:
    if echo is not None:
        echo(msg)
    else:
        logger.info(msg)


# ---------------------------------------------------------------------------
# Screenshot extension
# ---------------------------------------------------------------------------


def _extension_install_dir() -> Path:
    return Path.home() / ".local" / "share" / "gnome-shell" / "extensions" / _EXTENSION_UUID


def install_screenshot_extension(echo: Callable[[str], None] | None = None) -> bool:
    """Copy the bundled extension into place and enable it.

    Returns True if this *changed* anything (newly installed and/or newly
    enabled) — the caller uses that to decide whether a reboot is needed,
    since a hot-load of a brand-new extension isn't reliable on gnome-shell
    (see docs/gnome-screenshot-extension/README.md).
    """
    dest = _extension_install_dir()
    src = resources.files("automated_ken_runner") / "resources" / "gnome-screenshot-extension" / _EXTENSION_UUID
    changed = False

    if dest.exists():
        _say(echo, f"  screenshot extension already present at {dest}")
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(src), str(dest))
        _say(echo, f"  installed screenshot extension to {dest}")
        changed = True

    enabled = subprocess.run(
        ["gsettings", "get", "org.gnome.shell", "enabled-extensions"],
        capture_output=True, timeout=15, check=False,
    ).stdout.decode(errors="replace").strip()
    if _EXTENSION_UUID in enabled:
        _say(echo, "  screenshot extension already in enabled-extensions")
    else:
        # enabled is a GVariant array-of-strings repr, e.g. "['a', 'b']" —
        # splice our uuid in rather than fully reparsing it.
        if enabled.strip() in ("", "@as []", "[]"):
            new_value = f"['{_EXTENSION_UUID}']"
        else:
            new_value = enabled[:-1].rstrip() + f", '{_EXTENSION_UUID}']"
        result = subprocess.run(
            ["gsettings", "set", "org.gnome.shell", "enabled-extensions", new_value],
            capture_output=True, timeout=15, check=False,
        )
        if result.returncode == 0:
            _say(echo, "  enabled screenshot extension")
            changed = True
        else:
            _say(echo, f"  FAILED to enable screenshot extension: {result.stderr.decode(errors='replace')}")

    return changed


# ---------------------------------------------------------------------------
# Autologin
# ---------------------------------------------------------------------------


def enable_autologin(echo: Callable[[str], None] | None = None) -> bool:
    """Ensure GDM auto-logs this user in on boot. Returns True if changed."""
    username = getpass.getuser()
    config_path = next((p for p in _GDM_CONFIG_CANDIDATES if p.exists()), None)
    if config_path is None:
        _say(echo, "  could not find a GDM config file (custom.conf) — is this GDM3?")
        return False

    read = subprocess.run(
        ["sudo", "cat", str(config_path)], capture_output=True, timeout=15, check=False
    )
    if read.returncode != 0:
        _say(echo, f"  FAILED to read {config_path}: {read.stderr.decode(errors='replace')}")
        return False
    text = read.stdout.decode(errors="replace")

    if "AutomaticLoginEnable=True" in text and f"AutomaticLogin={username}" in text:
        _say(echo, f"  autologin already enabled for {username}")
        return False

    lines = text.splitlines()
    if "[daemon]" not in lines:
        lines.insert(0, "[daemon]")
    out_lines: list[str] = []
    daemon_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("AutomaticLoginEnable") or stripped.startswith("AutomaticLogin="):
            continue  # drop stale/conflicting values, we re-add them below
        out_lines.append(line)
        if stripped == "[daemon]":
            daemon_idx = len(out_lines) - 1
    insertion = ["AutomaticLoginEnable=True", f"AutomaticLogin={username}"]
    if daemon_idx is None:
        out_lines = ["[daemon]", *insertion, *out_lines]
    else:
        out_lines[daemon_idx + 1 : daemon_idx + 1] = insertion
    new_text = "\n".join(out_lines) + "\n"

    write = subprocess.run(
        ["sudo", "tee", str(config_path)],
        input=new_text.encode(), capture_output=True, timeout=15, check=False,
    )
    if write.returncode != 0:
        _say(echo, f"  FAILED to write {config_path}: {write.stderr.decode(errors='replace')}")
        return False
    _say(echo, f"  enabled autologin for {username} in {config_path}")
    return True


# ---------------------------------------------------------------------------
# Screen lock / blanking
# ---------------------------------------------------------------------------


def disable_screen_lock_and_blanking(echo: Callable[[str], None] | None = None) -> bool:
    """Turn off screen lock, idle blanking, and suspend-on-idle.

    None of these need a reboot to take effect, and none of them require
    sudo — they're per-user desktop settings, applied via gsettings on
    the runner's own session.
    """
    changed = False
    for schema, key, value in _SCREENSAVER_SETTINGS:
        current = subprocess.run(
            ["gsettings", "get", schema, key], capture_output=True, timeout=15, check=False
        ).stdout.decode(errors="replace").strip()
        if current == value:
            continue
        result = subprocess.run(
            ["gsettings", "set", schema, key, value],
            capture_output=True, timeout=15, check=False,
        )
        if result.returncode == 0:
            _say(echo, f"  set {schema} {key} -> {value}")
            changed = True
        else:
            _say(echo, f"  FAILED to set {schema} {key}: {result.stderr.decode(errors='replace')}")
    if not changed:
        _say(echo, "  screen lock/blanking already disabled")
    return changed


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def ensure_desktop_ready(echo: Callable[[str], None] | None = None) -> bool:
    """Run all desktop-setup steps. Returns True if a reboot is recommended.

    A reboot is only requested when something that needs a fresh session
    to take effect actually changed (new extension install/enable, or a
    fresh autologin config) — never on an already-configured machine, so
    this is safe to call on every ``prepare-machine`` and run-loop startup.
    """
    extension_changed = install_screenshot_extension(echo)
    autologin_changed = enable_autologin(echo)
    disable_screen_lock_and_blanking(echo)
    return extension_changed or autologin_changed
