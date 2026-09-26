"""One-time desktop-session setup for a newly-onboarded runner machine.

A runner needs an always-on, always-unlocked graphical session to test
GUI snaps non-interactively: autologin (so it comes back up ready after
any reboot/power loss with nobody at the keyboard), no screen lock, no
screen blanking/suspend (which would black out the very screen we need
to screenshot), no notification banners or "unlock keyring" dialogs
popping up over a running test and getting captured in its screenshot,
and the screenshot-capture GNOME Shell extension (see
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
_SYSTEM_AUTOSTART_DIR = Path("/etc/xdg/autostart")

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

# gsettings knobs to stop GNOME's own banner/pop-up notifications (the
# "Do Not Disturb" master switch and friends) from appearing on top of a
# window mid-test and getting captured in a screenshot.
_NOTIFICATION_SETTINGS = [
    ("org.gnome.desktop.notifications", "show-banners", "false"),
    ("org.gnome.desktop.notifications", "show-in-lock-screen", "false"),
    # GNOME Software's own "updates available" nagging is a separate
    # schema/banner from the generic notifications master switch above.
    ("org.gnome.software", "download-updates", "false"),
    ("org.gnome.software", "download-updates-notify", "false"),
]

# /etc/xdg/autostart/*.desktop entries that are known to pop dialogs/
# banners over whatever's on screen on a stock Ubuntu GNOME desktop, none
# of which a dedicated, unattended test runner needs:
#  - gnome-keyring-{secrets,pkcs11}: the actual source of the "Unlock
#    Keyring" dialog this was added for. GDM's PAM stack already tries to
#    auto-unlock the login keyring on autologin (see
#    /etc/pam.d/gdm-autologin's `pam_gnome_keyring.so` lines — already
#    shipped by Ubuntu, nothing for us to add there), but that only works
#    if the keyring's password happens to be blank; we have no safe way to
#    force an *existing* keyring to a blank password without knowing its
#    current one (and don't want to blindly delete a real one that might
#    hold real secrets). Not autostarting the daemon components that
#    prompt for unlock is the reliable, purely-additive fix instead: nothing
#    ever asks to unlock a keyring, so nothing ever prompts. A test runner
#    has no legitimate need for persisted secrets anyway.
#  - update-notifier / ubuntu-advantage-notification: Ubuntu's own
#    "updates available" / "Ubuntu Pro" nag dialogs.
#  - org.gnome.Evolution-alarm-notify: calendar/reminder pop-ups.
_NOISY_AUTOSTART_APPS = [
    "gnome-keyring-secrets",
    "gnome-keyring-pkcs11",
    "update-notifier",
    "ubuntu-advantage-notification",
    "org.gnome.Evolution-alarm-notify",
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
# Notification banners
# ---------------------------------------------------------------------------


def disable_notifications(echo: Callable[[str], None] | None = None) -> bool:
    """Turn off GNOME/GNOME Software notification banners.

    Same idempotent gsettings pattern as ``disable_screen_lock_and_blanking``
    — a banner (update available, calendar reminder, etc.) popping up over
    a test in progress gets captured in the screenshot just like the
    keyring dialog does.
    """
    changed = False
    for schema, key, value in _NOTIFICATION_SETTINGS:
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
        _say(echo, "  notification banners already disabled")
    return changed


# ---------------------------------------------------------------------------
# Noisy autostart apps (keyring unlock prompt, update nags, ...)
# ---------------------------------------------------------------------------


def _user_autostart_dir() -> Path:
    return Path.home() / ".config" / "autostart"


def disable_noisy_autostart_apps(echo: Callable[[str], None] | None = None) -> bool:
    """Stop known dialog/banner-producing apps from autostarting.

    Uses the standard per-user XDG autostart override mechanism (a
    ``~/.config/autostart/<id>.desktop`` with ``Hidden=true`` shadows the
    system-wide ``/etc/xdg/autostart/<id>.desktop`` for this user only) —
    purely additive, reversible (just delete the override file), and needs
    no sudo. A system entry that doesn't exist on this machine is silently
    skipped. See ``_NOISY_AUTOSTART_APPS`` for what and why.
    """
    changed = False
    dest_dir = _user_autostart_dir()
    for app_id in _NOISY_AUTOSTART_APPS:
        system_entry = _SYSTEM_AUTOSTART_DIR / f"{app_id}.desktop"
        if not system_entry.exists():
            continue
        dest = dest_dir / f"{app_id}.desktop"
        if dest.exists() and "Hidden=true" in dest.read_text(errors="replace"):
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest.write_text("[Desktop Entry]\nHidden=true\n")
        _say(echo, f"  disabled autostart of {app_id}")
        changed = True
    if not changed:
        _say(echo, "  noisy autostart apps already disabled")
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
    disable_notifications(echo)
    disable_noisy_autostart_apps(echo)
    return extension_changed or autologin_changed
