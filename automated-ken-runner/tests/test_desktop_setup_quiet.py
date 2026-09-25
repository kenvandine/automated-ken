"""Regression coverage for suppressing dialogs/banners that would get
captured in a test screenshot (keyring unlock prompt, update nags, ...).
"""

from unittest.mock import patch

from automated_ken_runner.desktop_setup import (
    _NOISY_AUTOSTART_APPS,
    disable_noisy_autostart_apps,
    disable_notifications,
)


def _fake_run():
    """Build a fake subprocess.run where `gsettings get` returns something
    that never matches the desired value, so every `set` is attempted and
    "succeeds".
    """

    class _Result:
        def __init__(self, stdout=b"", returncode=0):
            self.stdout = stdout
            self.returncode = returncode
            self.stderr = b""

    def run(cmd, **kwargs):
        if cmd[:2] == ["gsettings", "get"]:
            return _Result(stdout=b"'stale-value'\n")
        if cmd[:2] == ["gsettings", "set"]:
            return _Result(returncode=0)
        return _Result()

    return run


def test_disable_notifications_sets_every_knob():
    with patch("automated_ken_runner.desktop_setup.subprocess.run", side_effect=_fake_run()):
        changed = disable_notifications(echo=None)
    assert changed is True


def test_disable_notifications_noop_when_already_set():
    class _Result:
        def __init__(self, stdout):
            self.stdout = stdout
            self.returncode = 0
            self.stderr = b""

    def run(cmd, **kwargs):
        if cmd[:2] == ["gsettings", "get"] and cmd[2] == "org.gnome.desktop.notifications":
            return _Result(b"false\n") if cmd[3] in ("show-banners", "show-in-lock-screen") else _Result(b"'x'\n")
        if cmd[:2] == ["gsettings", "get"] and cmd[2] == "org.gnome.software":
            return _Result(b"false\n")
        return _Result(b"")

    with patch("automated_ken_runner.desktop_setup.subprocess.run", side_effect=run):
        changed = disable_notifications(echo=None)
    assert changed is False


def test_disable_noisy_autostart_apps_skips_missing_system_entries(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    fake_system_autostart = tmp_path / "etc-xdg-autostart-empty"
    fake_system_autostart.mkdir()
    with (
        patch("automated_ken_runner.desktop_setup.Path.home", return_value=fake_home),
        patch("automated_ken_runner.desktop_setup._SYSTEM_AUTOSTART_DIR", fake_system_autostart),
    ):
        # None of the system /etc/xdg/autostart/*.desktop files exist in
        # this sandbox, so nothing should be written and nothing changed.
        changed = disable_noisy_autostart_apps(echo=None)
    assert changed is False
    assert not (fake_home / ".config" / "autostart").exists()


def test_disable_noisy_autostart_apps_masks_present_entries(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    fake_system_autostart = tmp_path / "etc-xdg-autostart"
    fake_system_autostart.mkdir()
    for app_id in _NOISY_AUTOSTART_APPS:
        (fake_system_autostart / f"{app_id}.desktop").write_text("[Desktop Entry]\n")

    with (
        patch("automated_ken_runner.desktop_setup.Path.home", return_value=fake_home),
        patch("automated_ken_runner.desktop_setup._SYSTEM_AUTOSTART_DIR", fake_system_autostart),
    ):
        changed = disable_noisy_autostart_apps(echo=None)

    assert changed is True
    for app_id in _NOISY_AUTOSTART_APPS:
        override = fake_home / ".config" / "autostart" / f"{app_id}.desktop"
        assert override.exists()
        assert "Hidden=true" in override.read_text()

    # Re-running is a no-op once every override is already in place.
    with (
        patch("automated_ken_runner.desktop_setup.Path.home", return_value=fake_home),
        patch("automated_ken_runner.desktop_setup._SYSTEM_AUTOSTART_DIR", fake_system_autostart),
    ):
        changed_again = disable_noisy_autostart_apps(echo=None)
    assert changed_again is False
