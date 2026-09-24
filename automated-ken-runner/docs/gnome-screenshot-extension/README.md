# automated-ken-screenshot GNOME Shell extension

Exposes a trusted D-Bus method (`io.github.kenvandine.AutomatedKenScreenshot
.Screenshot`) that the runner calls (see `screenshot_capture.py`) to take a
full-screen screenshot of the real, autologin desktop session.

## Why this exists

GNOME Shell's own `org.gnome.Shell.Screenshot` D-Bus API refuses calls from
ordinary (non-allowlisted) processes with `AccessDenied: Screenshot is not
allowed` — confirmed this applies even to a `systemd --user` service running
in the same graphical session as the desktop. Code running *inside*
gnome-shell (an extension) is the same trusted process and has no such
restriction, so this extension just re-exposes the capability under its own
D-Bus name for the runner to call non-interactively.

## Install

```
mkdir -p ~/.local/share/gnome-shell/extensions
cp -r automated-ken-screenshot@kenvandine.github.io \
    ~/.local/share/gnome-shell/extensions/
gsettings set org.gnome.shell enabled-extensions \
    "$(gsettings get org.gnome.shell enabled-extensions | sed "s/\]$/, 'automated-ken-screenshot@kenvandine.github.io']/;s/^\[, /[/")"
```

## Known issue: not yet confirmed to hot-load

On the Ubuntu 26.04 / GNOME Shell 50.1 test host, neither `gnome-extensions
install/enable` nor a direct `gsettings set org.gnome.shell
enabled-extensions` caused the *already-running* gnome-shell process to pick
up the extension — `gnome-extensions list` (even with `--user`/`--system`)
returned none of the manually-installed files, and `org.gnome.Shell.Eval`
(used to introspect `Main.extensionManager` directly) is disabled on this
build, so this couldn't be root-caused further without disrupting the live
session. `automated_ken_runner.screenshot_capture` raises a clear
`ScreenshotCaptureError` if the extension's D-Bus name isn't present, rather
than failing silently — that error is a strong signal this needs a session
logout/login (or an equivalent gnome-shell restart) to pick up a
newly-installed extension for the first time. Once installed and confirmed
loaded (e.g. `gnome-extensions list` shows it as enabled after a restart),
no further re-installation should be needed for subsequent runner-service
restarts.
