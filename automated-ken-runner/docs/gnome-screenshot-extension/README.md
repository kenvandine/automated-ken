# automated-ken-screenshot GNOME Shell extension

Exposes a trusted D-Bus method (`io.github.kenvandine.AutomatedKenScreenshot
.Screenshot`) that the runner calls (see `screenshot_capture.py`) to take a
full-screen screenshot of the real, autologin desktop session.

The canonical, shipped copy of this extension's source lives in
`src/automated_ken_runner/resources/gnome-screenshot-extension/` (it's
packaged inside the runner snap so `prepare-machine` can install it with no
extra deploy step — see `desktop_setup.py`). This directory only holds
docs; edit the source there, not here.

## Why this exists

GNOME Shell's own `org.gnome.Shell.Screenshot` D-Bus API refuses calls from
ordinary (non-allowlisted) processes with `AccessDenied: Screenshot is not
allowed` — confirmed this applies even to a `systemd --user` service running
in the same graphical session as the desktop. Code running *inside*
gnome-shell (an extension) is the same trusted process and has no such
restriction, so this extension just re-exposes the capability under its own
D-Bus name for the runner to call non-interactively.

## Install

Handled automatically by `automated-ken-runner prepare-machine` (see
`desktop_setup.py`) — installs the extension, enables autologin, disables
screen lock/blanking, and reboots the machine if anything changed so
gnome-shell picks it all up fresh. Manual install, if ever needed:

```
mkdir -p ~/.local/share/gnome-shell/extensions
cp -r automated-ken-screenshot@kenvandine.github.io \
    ~/.local/share/gnome-shell/extensions/
gsettings set org.gnome.shell enabled-extensions \
    "$(gsettings get org.gnome.shell enabled-extensions | sed "s/\]$/, 'automated-ken-screenshot@kenvandine.github.io']/;s/^\[, /[/")"
```

## Known issue: needs a fresh gnome-shell session to hot-load

On the Ubuntu 26.04 / GNOME Shell 50.1 test host, neither `gnome-extensions
install/enable` nor a direct `gsettings set org.gnome.shell
enabled-extensions` caused the *already-running* gnome-shell process to pick
up the extension — `gnome-extensions list` (even with `--user`/`--system`)
returned none of the manually-installed files, and `org.gnome.Shell.Eval`
(used to introspect `Main.extensionManager` directly) is disabled on this
build, so this couldn't be root-caused further without disrupting the live
session. `automated_ken_runner.screenshot_capture` raises a clear
`ScreenshotCaptureError` if the extension's D-Bus name isn't present, rather
than failing silently. This is why `prepare-machine` reboots the machine
(with autologin enabled, it comes back up ready — no manual login needed)
right after installing the extension for the first time, instead of relying
on a hot-load that doesn't work on this build. Once installed and confirmed
loaded (`gnome-extensions list` shows it enabled after the reboot), no
further reinstall/reboot happens on subsequent `prepare-machine` runs.

