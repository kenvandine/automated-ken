"""Native, full-screen screenshot capture on the runner's real desktop session.

Unlike YARF/Robot Framework's platform layer (which needs wlroots-specific
input-injection protocols GNOME/Mutter never implements), this captures a
screenshot of whatever is on-screen right now on the host's live, autologin
desktop session — no virtual/headless display involved.

GNOME Shell's own ``org.gnome.Shell.Screenshot`` D-Bus API refuses calls
from ordinary (non-allowlisted) callers with "Screenshot is not allowed",
even from a systemd --user service in the same graphical session. To work
around that, a small trusted GNOME Shell extension
(``automated-ken-screenshot@kenvandine.github.io`` — see
``docs/gnome-screenshot-extension/`` in this repo) runs *inside* gnome-shell
and re-exposes the same capability over its own D-Bus name, which has no
such restriction since it's the same trusted process taking the shot.

This module is intentionally the *only* place that knows how a screenshot
gets taken — if the capture mechanism ever needs to change (a different
portal, a future GNOME API, ...), only this file should need to change.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_BUS_NAME = "io.github.kenvandine.AutomatedKenScreenshot"
_OBJECT_PATH = "/io/github/kenvandine/AutomatedKenScreenshot"
_METHOD = f"{_BUS_NAME}.Screenshot"
_TIMEOUT_SECONDS = 30


class ScreenshotCaptureError(RuntimeError):
    """Raised when a native screenshot capture attempt fails outright."""


def capture_screenshot(dest_path: Path) -> bytes:
    """Capture the current full screen to ``dest_path`` and return its bytes.

    Raises ``ScreenshotCaptureError`` (with a message pointing at the likely
    cause — extension not installed/enabled, or gnome-shell not running)
    rather than returning a sentinel, so callers can't silently treat a
    failed capture as a passing/blank test result.
    """
    proc = subprocess.run(
        [
            "gdbus", "call", "--session",
            "--dest", _BUS_NAME,
            "--object-path", _OBJECT_PATH,
            "--method", _METHOD,
            str(dest_path),
        ],
        capture_output=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode(errors="replace").strip()
        raise ScreenshotCaptureError(
            "native screenshot capture failed — is the "
            f"automated-ken-screenshot@kenvandine.github.io GNOME Shell "
            f"extension installed and enabled on this desktop session? "
            f"({stderr or 'no error output'})"
        )
    stdout = (proc.stdout or b"").decode(errors="replace")
    if "true" not in stdout.lower():
        raise ScreenshotCaptureError(f"screenshot extension reported failure: {stdout.strip()}")
    if not dest_path.exists():
        raise ScreenshotCaptureError(f"screenshot extension reported success but {dest_path} is missing")
    return dest_path.read_bytes()
