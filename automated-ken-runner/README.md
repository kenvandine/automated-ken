# automated-ken-runner

A test-execution agent for [Automated Ken](../README.md). Install it on a
dedicated desktop or laptop and enroll it with your dashboard; it then runs
queued snap tests in the machine's real logged-in desktop session and
reports results, screenshots and logs back.

Setup steps are in [`GETTING_STARTED.md`](../GETTING_STARTED.md#part-2-runner-machines).

## How it works

- **Outbound only.** The runner long-polls the dashboard for work and pushes results; it never listens on a port, so it works behind NAT.
- **Architecture matching.** It reports its architecture (from `uname -m`, in snap naming: `amd64`, `arm64`, …) at enrollment and on every heartbeat. The dashboard only gives it jobs for that architecture, from the user it's enrolled under.
- **Idle-only.** It only takes a job when the desktop has been idle for 2 minutes and isn't locked (`loginctl`, plus GNOME Mutter's idle monitor), so it never disturbs someone using the machine.
- **Heartbeats.** Sent every 15 seconds on a background thread, including while a job runs. The dashboard shows a runner as offline after 90 seconds without one.
- **Cancellation.** If you cancel a running job on the dashboard's **Runners** page, the next heartbeat picks it up, the test process is stopped, and the job is reported as `cancelled`.

For each job it:

1. installs or refreshes the snap from the job's channel with `sudo snap install|refresh --channel=…`;
2. downloads the snap's YARF suite (`tests/suite/` in its packaging repo) and runs it with `yarf` — on the real session (`Mir` platform) when a display is available — for up to 30 minutes;
3. if there's no suite, launches the app, waits for it to render, and captures a screenshot (a smoke test);
4. checks screenshots aren't blank, uploads them and the log, and reports `passed`/`failed`.

## Requirements

- Ubuntu with GNOME, logged into a graphical session (autologin is set up for you)
- `snapd`, and passwordless `sudo` for `snap` (jobs run from a background service with no terminal):
  ```bash
  echo "$USER ALL=(root) NOPASSWD: /usr/bin/snap" | sudo tee /etc/sudoers.d/automated-ken-runner
  ```
- A machine dedicated to testing — preparation disables the screen lock

## Commands

| Command | Purpose |
|---------|---------|
| `automated-ken-runner prepare-machine [--no-reboot]` | Install YARF, check the `sudo` rule, install the screenshot extension, enable autologin, disable screen lock/blanking; reboots if the desktop setup changed |
| `automated-ken-runner enroll --server URL --token TOKEN [--name NAME]` | Register with a dashboard using a one-time token from its **Runners** page |
| `automated-ken-runner status` | Show enrollment and whether it would take a job right now |
| `automated-ken-runner run` | Run the job loop in the foreground |
| `automated-ken-runner unenroll` | Forget the stored credentials |

The job loop normally runs as a user service:

```bash
systemctl --user enable --now snap.automated-ken-runner.run.service
journalctl --user -u snap.automated-ken-runner.run.service -f
```

Credentials are stored in `$SNAP_USER_COMMON/config.json` (mode 0600).

## Why classic confinement

The runner installs arbitrary snaps, launches GUI apps in the live session,
talks to the session D-Bus and compositor to capture screenshots, and reads
logind idle state — none of which strict confinement can grant. Because
classic snaps don't get the base snap's runtime, `snapcraft.yaml` stages its
own Python interpreter.

Screenshots come from a small GNOME Shell extension shipped in the snap,
since GNOME's own screenshot D-Bus API refuses non-allowlisted callers —
see [`docs/gnome-screenshot-extension/`](docs/gnome-screenshot-extension/README.md).

## Development

```bash
pip install -e .
automated-ken-runner --help
snapcraft pack        # build the snap
```

## License

GPL-3.0-or-later — see [`LICENSE`](../LICENSE).
