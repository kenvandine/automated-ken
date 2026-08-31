"""Regression tests for SESSION_SECRET persistence across restarts.

Previously web/app.py read SESSION_SECRET straight from os.environ and
minted a brand-new random secret on every process start if it wasn't set.
That meant:

  - Every restart of the serve daemon logged every user out (session
    cookies are signed with the secret; a new secret invalidates them all).
  - Setting `snap set snap-dashboard github-client-id=...` per the README
    had no effect on session stability, because the snap's configure hook
    never wrote a session secret into config.env in the first place, and
    even if it had, app.py wasn't reading config.env at all for this value.

app.py now goes through snap_dashboard.config (the same config.env /
env-var loading used everywhere else) and persists a generated secret to
config.env the first time there isn't one, so it's stable across restarts.

These tests spawn a real subprocess for each case (rather than importing
snap_dashboard.web.app in-process) because the secret is decided at module
import time as a side effect of the environment — the only reliable way to
test "what happens on process start" is to actually start a process.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_IMPORT_AND_PRINT_SECRET = (
    "import snap_dashboard.web.app as app; print(app._session_secret)"
)


def _run(home: Path, extra_env: dict[str, str] | None = None) -> str:
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        # Keep each subprocess's DB isolated too, though these tests don't
        # touch it — avoids any chance of writing to a real dev database.
        "SNAP_DASHBOARD_DB": str(home / "test.db"),
    }
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_AND_PRINT_SECRET],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1]


def test_session_secret_persists_across_restarts(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    first_secret = _run(home)
    second_secret = _run(home)  # simulates a daemon restart

    assert first_secret == second_secret
    assert len(first_secret) >= 32

    config_env = home / ".local" / "share" / "snap-dashboard" / "config.env"
    assert config_env.exists()
    assert f"SESSION_SECRET={first_secret}" in config_env.read_text()


def test_explicit_session_secret_env_var_is_not_overwritten(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    secret = _run(home, extra_env={"SESSION_SECRET": "my-explicit-secret"})

    assert secret == "my-explicit-secret"
    # An explicitly-provided secret should not trigger a write to disk.
    config_env = home / ".local" / "share" / "snap-dashboard" / "config.env"
    assert not config_env.exists()


def test_different_users_get_different_generated_secrets(tmp_path: Path) -> None:
    """Sanity check that we're generating real randomness, not a constant."""
    home_a = tmp_path / "home_a"
    home_b = tmp_path / "home_b"
    home_a.mkdir()
    home_b.mkdir()

    assert _run(home_a) != _run(home_b)
