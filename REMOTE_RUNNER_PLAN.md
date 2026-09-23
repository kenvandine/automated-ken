# Remote Test Runner — Private On-Machine YARF Execution

## Status

Planning document. Nothing in this file is implemented yet. Mirrors the
style of [`AGENTIC_PLAN.md`](AGENTIC_PLAN.md) — phased, independently
shippable, with explicit DB changes per phase.

## Motivation

Today YARF tests run exclusively on GitHub-hosted Actions runners
(`.github/workflows/snap-test.yml` in the testing repo). Because those
runners have no real desktop session, the workflow has to fake one:
`mir-test-tools.demo-server` (a virtual Mir/Wayland compositor),
`llvmpipe` software rendering, a synthetic systemd user session, and a
Python script that extracts screenshots out of YARF's `log.html` by
regex-hunting for zlib+base64 blobs. This works, but:

- it's not a real desktop, GPU, or user session — it catches some
  regressions and misses others (theming, real GPU driver quirks, HiDPI,
  actual window manager behaviour)
- **screenshots never actually make it back to the dashboard today.**
  The Python orchestration code (`baselines.py`, `screenshot_reviewer.py`,
  `pr_viewer.py`) is built around a PR-with-embedded-screenshots model
  that the deployed workflow abandoned in favor of
  `actions/upload-artifact` — and nothing in `snap_dashboard/` calls the
  GitHub Actions Artifacts API. So the "LLM compares before/after
  screenshots" feature is currently dead code against real runs; only
  the pass/fail exit code makes it back (via direct Actions API status
  polling).

A private runner installed on an idle desktop/laptop with an unlocked
screen fixes both problems at once: real hardware/desktop for higher-
fidelity tests, and a direct upload path for screenshots straight into
the dashboard's database — no PR, no artifact-download plumbing needed.

It should feel like installing a self-hosted GitHub Actions runner:
install a small agent, run one enrollment command with a token generated
in the web UI, and the machine shows up as a manageable resource.

## Architecture

```
┌────────────────────────────────────────────────────────────────┐
│                      snap-dashboard (server)                    │
│                                                                  │
│  /runners page          /api/runners/*  (bearer-token auth,     │
│  (session-cookie auth)   separate from browser session cookies) │
│       │                        ▲            │                  │
│       │ generate                │ heartbeat  │ dispatch/results │
│       │ enrollment token        │ poll job   │                  │
│       ▼                        │            ▼                  │
│  ┌─────────────┐   SQLite: runners, test_runs                   │
│  │  Runner rows │              (+ dispatch_target/runner_id),   │
│  └─────────────┘               test_run_screenshots             │
└──────────────────────────────────┬───────────────────────────────┘
                                   │ HTTPS, outbound only from the runner
                                   │ (poll/long-poll — no inbound port,
                                   │  works behind NAT/home routers)
                    ┌──────────────┴───────────────┐
                    │   automated-ken-runner        │
                    │   (installed on an idle       │
                    │    desktop/laptop, real        │
                    │    logged-in graphical session)│
                    │                                │
                    │  1. idle/lock detection loop    │
                    │  2. poll for a queued job        │
                    │  3. snap install/refresh snap    │
                    │  4. run yarf against the REAL    │
                    │     desktop session              │
                    │  5. extract + validate screenshots│
                    │  6. upload status + screenshots  │
                    └────────────────────────────────┘
```

Existing GitHub-Actions-based dispatch is untouched and remains the
default/fallback path — this adds a second `dispatch_target`, not a
replacement.

## New Configuration Values

| Key | Purpose | Default |
|-----|---------|---------|
| `UserConfig.prefer_remote_runner` | Try a remote runner before falling back to GitHub Actions | `false` |
| `UserConfig.runner_job_timeout_minutes` | Mark a claimed-but-stalled job as `error` after this long | `10` |
| `Runner.idle_threshold_seconds` (per-runner, set by the runner itself) | How long the desktop must be untouched before it reports "idle" | `120` |

## New Database Models

### `runners` — registered runner machines
```
id, user_id, name, secret_hash, enrollment_token_hash, enrollment_expires_at,
arch, os_name, desktop_env,                      -- reported at enroll/heartbeat
status (enrolling|idle|locked|busy|offline),
idle_seconds, last_heartbeat_at,
current_test_run_id (FK -> test_runs, nullable),
created_at, revoked_at
```

### `test_run_screenshots` — persistent screenshot storage (source-agnostic)
```
id, test_run_id (FK -> test_runs), image_name, image_b64, width, height,
brightness_mean, is_valid (bool), captured_at
```
This table is the actual fix for the dead screenshot pipeline described
above. Both dispatch targets write into it:
- Remote runner: uploads directly via `POST .../screenshots`.
- GitHub Actions (separate, smaller follow-up, see "Also worth doing"
  below): a small addition to `sync_test_runs()`/`poll_for_gh_run_id()`
  to download the `yarf-results-*` artifact zip and populate the same
  table, instead of relying on the currently-nonexistent PR flow.

`baselines.py::load_test_run_screenshots()` is refactored to read this
table first, for any `TestRun` regardless of `dispatch_target`.

### `test_runs` (extend existing model)
```
+ dispatch_target   VARCHAR(32)  DEFAULT 'github_actions'   -- or 'remote_runner'
+ runner_id         INTEGER FK -> runners.id, nullable
```
Deliberately extending `TestRun` rather than inventing a parallel job
table — the whole downstream pipeline (promotion, `ScreenshotReviewerAgent`,
`/testing` UI, auto-promote) keeps working unchanged for both dispatch
targets; only trigger/poll/result-ingestion logic differs.

---

## Phase R1 — Data model + dispatch abstraction

**Goal:** Land the schema without behavior changes yet.

- `db/models.py`: add `Runner`, `TestRunScreenshot`; extend `TestRun`
  with `dispatch_target`/`runner_id`.
- `db/session.py`: additive migrations (same pattern as existing
  `_migrate()`).
- Refactor `baselines.py::load_test_run_screenshots()` to check
  `TestRunScreenshot` first, falling back to the existing PR-based path
  (kept as a fallback, not deleted — some historical runs may still have
  PR-embedded screenshots).

## Phase R2 — Runner registration + auth

**Goal:** A machine can enroll and be recognized, nothing runs yet.

- New route module `web/routes/runners.py`:
  - `GET /runners` (session auth) — list this user's runners + status.
  - `POST /runners/new` — generates a one-time enrollment token (random,
    hashed at rest, ~15 min expiry), shows the exact enrollment command
    to copy onto the target machine (mirrors GitHub's own self-hosted
    runner "Settings → Actions → Runners → New runner" UX).
  - `POST /runners/{id}/revoke` — invalidates the runner's secret.
- New unauthenticated-by-session, bearer-token-authenticated API
  namespace `web/routes/runner_api.py` (`/api/runners/*`):
  - `POST /api/runners/enroll` — body: `{enrollment_token, name, arch,
    os_name, desktop_env}`. Validates + consumes the enrollment token,
    creates the `Runner` row, returns `{runner_id, secret}` **once**
    (never retrievable again, same UX as a PAT).
  - A FastAPI dependency `get_runner_from_bearer(request)` resolving
    `Runner` by `secret_hash`, for all other `/api/runners/*` endpoints.
- New CLI tool (separate package, see Phase R7): `automated-ken-runner
  enroll --server <url> --token <token>` performs the enroll call and
  persists `{server_url, runner_id, secret}` locally.

## Phase R3 — Heartbeat + idle/lock detection

**Goal:** The dashboard knows, live, which runners exist and whether
they're actually available (idle + unlocked) without disturbing anyone.

- Runner daemon loop (every ~15s): compute idle state via
  `loginctl show-session $XDG_SESSION_ID -p LockedHint -p IdleHint`
  (systemd-logind, works on GNOME/KDE/anything using logind) with a
  GNOME Mutter `org.gnome.Mutter.IdleMonitor` D-Bus fallback for a more
  precise idle-seconds figure. POSTs
  `PATCH /api/runners/{id}/heartbeat` — `{status, idle_seconds, locked}`.
- Server marks a runner `offline` if no heartbeat received for >90s
  (checked lazily on read, no extra background job needed initially).
- `/runners` page shows a live status pill per runner; reuse the
  existing SSE plumbing (`agents/runner.py::ActivityTracker`,
  `/api/events`) to push status changes instead of polling, consistent
  with how the Agents page already works.

### Idle detection vs. "allowed to lock/suspend at all" — these are two different knobs

`LockedHint`/`IdleHint`/keyboard-and-mouse-idle-seconds answer "is a
human actively using this machine right now" — that's the right signal
for "don't steal the machine out from under someone." It says nothing
about whether the *OS* is configured to auto-lock the screen or suspend
after its own inactivity timeout, which is a completely separate GNOME
power/screensaver setting.

For a machine that's meant to sit there as a **dedicated, always-
available test device**, the OS-level auto-lock/auto-suspend timeouts
need to be **disabled entirely** (see "Runner machine preparation"
under Phase R7) — otherwise the machine locks itself or suspends after
N idle minutes and becomes permanently unavailable for dispatch the
moment nobody's touched it in a while, which defeats the entire point.
The runner's own idle/lock heartbeat still matters on a machine
configured this way — it's the difference between "nobody's touched
this in 3 minutes, safe to run tests" and "someone just sat down and
is actively using it, don't interrupt them" — but it should very rarely
observe `locked=true` if the machine is prepared correctly.

## Phase R4 — Job queue + dispatch

**Goal:** A queued `TestRun` can be claimed and picked up by an eligible
runner instead of (or in addition to) GitHub Actions.

- `/testing` page: per-snap "Run on" control — `GitHub Actions` (default)
  or a specific registered runner, or `Any available runner`.
- `testing/orchestrator.py::trigger_remote_run(...)` — sibling to
  `trigger_workflow()`. Creates `TestRun` with
  `status="queued", dispatch_target="remote_runner", runner_id=<selected
  or NULL>`. No outbound network call here — the runner will come and
  get it.
- `GET /api/runners/{id}/next-job` (long-poll, ~25s): server looks for a
  `TestRun` where `dispatch_target="remote_runner"` and
  `(runner_id = self OR runner_id IS NULL)` and `status="queued"`, **and**
  the calling runner's last-reported status is `idle` (never assign to a
  busy/locked runner). Claims it atomically (`runner_id = self,
  status="triggered"`), returns
  `{test_run_id, snap_name, channel, architecture, suite_url}`.
- `GET /api/runners/{id}/jobs/{job_id}/suite` — the dashboard (which
  already holds the user's GitHub token) fetches
  `suites/<snap>/suite/**` from `UserConfig.testing_repo` via the GitHub
  Contents API (same pattern as `snapcraft/fetcher.py`), zips it
  in-memory, and streams it back. **The runner machine never needs its
  own GitHub credential.**
- Fallback: if `prefer_remote_runner` is on but no runner is idle within
  a short timeout, fall back to `trigger_workflow()` (GitHub Actions)
  automatically.

## Phase R5 — Execution on the runner

**Goal:** Actually run the suite against the real desktop session and
get validated screenshots back.

Reuses the proven sequence from the production workflow, adapted for a
real logged-in session (no synthetic compositor needed — the runner
*is* the desktop):

1. `snap install <snap> --channel <channel>` (or `refresh` if already
   present).
2. Launch it in the current graphical session — just `snap run <snap>`,
   inheriting the real `WAYLAND_DISPLAY`/`DISPLAY`, real GPU, real theme.
3. `yarf --platform <Wayland-or-X11, whatever this desktop actually
   runs> --outdir <dir> <downloaded suite>`.
4. Extract screenshots from `log.html` using the **same** zlib/base64
   extraction + brightness-validity heuristic already proven in the
   production workflow — factor that Python snippet out of the inline
   workflow script into a small shared module
   (`automated_ken_runner/screenshots.py`) so it has one implementation
   instead of drifting between CI and the runner.
5. Clean up: close the app under test so the desktop returns to a clean
   idle state for the human. `snap remove` is a configurable option
   (default: keep it installed, for faster subsequent runs).
6. Report:
   - `PATCH /api/runners/{id}/jobs/{job_id}` — status
     (`running`/`passed`/`failed`), `yarf_exit_code`, timestamps.
   - `POST /api/runners/{id}/jobs/{job_id}/screenshots` — multipart PNG
     upload(s), written into `test_run_screenshots`.
   - Optionally raw `log.html`/stdout/stderr for debugging, stored
     wherever `TestRun` logs already go (or a simple text column).

Once `TestRun.status` flips to `passed`/`failed`, everything downstream
(`_maybe_submit_auto_promoter`, `ScreenshotReviewerAgent`, promotion)
runs exactly as it does for GitHub-Actions-sourced runs today — no
changes needed there, since it only depends on `TestRun`/screenshot
tables, not on how the run got there.

## Phase R6 — Safety, concurrency, abort handling

**Goal:** Never surprise the human sitting at (or walking up to) the
machine.

- A runner only *claims* a job while it last reported `idle` (unlocked +
  no input for `idle_threshold_seconds`). It re-checks idle state
  immediately before actually launching the snap, as a last-second
  guard against a race with the human sitting down between poll and
  claim.
- If the machine locks or gets real user input mid-run: default policy
  is to let the already-started run finish (YARF suites are short,
  usually well under the `runner_job_timeout_minutes` ceiling) rather
  than yank the test out from under a real person; a hard timeout still
  applies regardless.
- One job at a time per runner — enforced both by the runner (it simply
  doesn't poll for a new job while one is in flight) and the server
  (won't hand out a second job to a runner whose `current_test_run_id`
  is already set).
- If a runner goes silent (no heartbeat) while holding a claimed job
  past `runner_job_timeout_minutes`, the server marks that `TestRun` as
  `error` and — if `prefer_remote_runner` — automatically retries via
  GitHub Actions.

## Phase R7 — Packaging: a Debian package

**Goal:** Make installing this as easy as installing a GitHub self-
hosted runner, shipped as a `.deb` — matching the exact packaging
convention already established in [`ailab`](https://github.com/lemonade-sdk/ailab)
(same author, same target platform: Ubuntu + snapd + systemd `--user`
services), rather than inventing a new one.

### Layout

New top-level directory in this repo, `runner/`, sibling to
`snap-dashboard/` — same monorepo, different install target:

```
runner/
├── pyproject.toml              setuptools + setuptools-scm, dynamic version
├── automated_ken_runner/
│   ├── __init__.py
│   ├── cli.py                  click CLI: enroll, run, prepare-machine, status
│   ├── config.py               reads/writes ~/.config/automated-ken-runner/
│   ├── idle.py                 loginctl / logind idle+lock detection
│   ├── client.py                httpx client for /api/runners/* (bearer auth)
│   ├── executor.py              snap install/run + yarf invocation
│   └── screenshots.py            zlib/base64 log.html extraction (shared with
│                                  the CI workflow's inline script — see below)
├── debian/
│   ├── control                  debhelper-compat (=13), dh-python,
│   │                            pybuild-plugin-pyproject, python3-all, +
│   │                            Depends: snapd, systemd, python3-click,
│   │                            python3-httpx, python3-pil
│   ├── rules                     dh $@ --buildsystem=pybuild --with python3;
│   │                             SETUPTOOLS_SCM_PRETEND_VERSION from changelog
│   ├── changelog
│   ├── copyright
│   ├── automated-ken-runner.install     -> ships the systemd user unit
│   ├── automated-ken-runner.service     systemd --user unit (see below)
│   └── automated-ken-runner.postinst    snapd presence check + enable hint
└── README.md
```

### systemd unit (`debian/automated-ken-runner.service`)

```ini
[Unit]
Description=Automated Ken remote test runner
Documentation=https://github.com/kenvandine/automated-ken
After=network.target graphical-session.target

[Service]
ExecStart=/usr/bin/automated-ken-runner run
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

A `systemd --user` unit (not system-wide) is the right call here, not
just convention-matching: the runner needs to `snap run` GUI apps into
the *logged-in graphical session* it's checking idle-state for, and
needs that session's `WAYLAND_DISPLAY`/`DBUS_SESSION_BUS_ADDRESS` — a
system service would need to fight its way into the user session anyway
(`machinectl shell`, manual bus address plumbing) for no benefit.

### `debian/control`

```
Source: automated-ken-runner
Section: utils
Priority: optional
Maintainer: Ken VanDine <ken@vandine.org>
Build-Depends:
 debhelper-compat (= 13),
 dh-python,
 pybuild-plugin-pyproject,
 python3-all,
 python3-setuptools,
 python3-setuptools-scm
Standards-Version: 4.7.2
Rules-Requires-Root: no
Homepage: https://github.com/kenvandine/automated-ken

Package: automated-ken-runner
Architecture: all
Depends:
 ${misc:Depends},
 ${python3:Depends},
 python3-click,
 python3-httpx,
 python3-pil,
 snapd,
 systemd,
 util-linux-extra
Description: Private test runner for Automated Ken
 Registers this machine as a private test-execution resource for an
 Automated Ken dashboard instance. Polls for queued YARF test jobs,
 installs and runs the snap under test in the real logged-in desktop
 session (not a headless/faked compositor), captures and validates
 screenshots, and reports results back — comparable to installing a
 self-hosted GitHub Actions runner.
 .
 Only claims jobs while the desktop is unlocked and idle, so it never
 interrupts active use of the machine.
```

### `debian/automated-ken-runner.postinst`

Mirrors `ailab.postinst`'s style: check for `snapd` (hard requirement,
since job execution is entirely `snap install`/`snap run`), print the
enable-service hint rather than auto-enabling (a `systemd --user` unit
can't be usefully enabled from a root postinst script running outside
any user session anyway):

```sh
#!/bin/sh
set -eu
case "$1" in
    configure)
        if ! command -v snap >/dev/null 2>&1; then
            echo "automated-ken-runner: snapd is required." >&2
            exit 1
        fi
        echo ""
        echo "automated-ken-runner installed. Next steps:"
        echo "  automated-ken-runner enroll --server <url> --token <token>"
        echo "  systemctl --user enable --now automated-ken-runner"
        echo ""
        ;;
esac
#DEBHELPER#
```

### CI (mirrors `ailab/.github/workflows/ci.yml`)

Add jobs to the existing `.github/workflows/ci.yml` in this repo (or a
new workflow scoped to `runner/`, triggered on paths):
- `ruff check runner/automated_ken_runner/`
- smoke test: `pip install -e runner/`, import the package, `automated-ken-runner --help`
- `lintian` on an unsigned source build (`debuild -us -uc -S -d` from
  `runner/`, then `lintian --fail-on error ../*.changes`)

### Release (mirrors `ailab/.github/workflows/release-ppa.yml`)

Same pattern: a `release: [published]`-triggered workflow that stamps
`debian/changelog` per target distro (noble/questing/resolute), builds a
signed source package with `debuild`, and `dput`s to a Launchpad PPA
(e.g. `ppa:ken-vandine/automated-ken-runner`) — reusing the same
`GPG_PRIVATE_KEY`/`GPG_PASSPHRASE`/`GPG_KEY_ID` org secrets already set
up for `ailab`, if this repo has access to them, otherwise new ones
scoped to this repo.

### Runner machine preparation: disabling screen-lock and auto-suspend

A machine dedicated to this purpose needs GNOME's own idle-lock and
auto-suspend timeouts turned off — otherwise it locks or suspends itself
after N idle minutes and permanently disqualifies itself from dispatch
(see the "Idle detection vs. allowed to lock/suspend at all" note under
Phase R3). Doing this by hand across several laptops is exactly the kind
of fiddly, easy-to-forget setup step that should be automated rather
than left as a manual checklist — add it as a CLI subcommand:

```sh
automated-ken-runner prepare-machine
```

which runs (and is idempotent/safe to re-run):

```sh
# Never blank/lock the screen or suspend due to inactivity
gsettings set org.gnome.desktop.session idle-delay 0
gsettings set org.gnome.desktop.screensaver lock-enabled false
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-battery-type 'nothing'
# Keep the display itself powered on too — DPMS-off would break screenshot capture
gsettings set org.gnome.settings-daemon.plugins.power idle-dim false
# If it's a laptop: don't suspend on lid-close either (assumes it stays
# plugged in and open, or docked/lid-open on a stand)
gsettings set org.gnome.settings-daemon.plugins.power lid-close-ac-action 'nothing'
```

`enroll` should call `prepare-machine` automatically (with a `--skip-power-settings`
escape hatch for anyone who wants to manage this themselves, e.g. via
`dconf` policy on a fleet), and print a clear warning either way:

> ⚠️  This machine's screen will no longer lock or auto-suspend. Only do
> this on a dedicated test device with no sensitive data logged in, in a
> physically secured location — anyone with physical access now has full
> access to whatever's logged in.

That last point is a genuine security tradeoff, not just a courtesy
note — worth stating plainly in both the CLI output and the docs.

### `/runners` UX

- "Add runner" flow shows the exact install + enroll commands, e.g.:
  ```sh
  sudo add-apt-repository ppa:ken-vandine/automated-ken-runner
  sudo apt install automated-ken-runner
  automated-ken-runner enroll --server https://dashboard.example.com --token r_AbC123...
  systemctl --user enable --now automated-ken-runner
  ```
- `/runners/{id}` detail page: recent jobs run on that machine, last
  screenshots, revoke button — same shape as `/version-bumps/{id}`.
- `/testing` run-history rows get a "Ran on: <runner name>" vs "Ran on:
  GitHub Actions" badge.

---

## Open Questions (need a decision before/while implementing)

1. ~~Packaging format for `automated-ken-runner`.~~ **Resolved: `.deb`**,
   matching the `ailab` convention exactly (debhelper + pybuild +
   setuptools-scm + systemd `--user` unit + Launchpad PPA release flow).
   See Phase R7 above for the concrete layout.
2. **Desktop environment scope for v1.** `loginctl`-based idle/lock
   detection works generically via logind; the GNOME Mutter D-Bus
   idle-monitor fallback is GNOME-specific. Fine to scope v1 to
   GNOME/Ubuntu desktops (matches your own machines) and generalize
   later, or worth designing the idle-check as a pluggable interface
   from day one?
3. **Fleet size expectations.** Just your own machine(s) for now, or
   should the `/runners` page and permission model anticipate other
   people registering their own machines against your dashboard
   instance later? (Affects whether runner registration needs
   allowlisting/approval beyond "logged-in user generated a token".)
4. **Suite fetch mechanism.** Proposed: dashboard proxies the specific
   `suites/<snap>/suite/` directory from the testing repo via the
   GitHub Contents API and zips it on the fly. Alternative: the runner
   does a shallow `git clone` of the whole testing repo itself (simpler
   code, but now the runner machine needs its own scoped GitHub read
   token, and re-clones/pulls the whole repo instead of just the one
   suite). Proxying is my default recommendation (keeps runner
   credential-free), but flagging the tradeoff.
5. **PPA access.** Does this repo's GitHub Actions have access to the
   same `GPG_PRIVATE_KEY`/`GPG_PASSPHRASE`/`GPG_KEY_ID` secrets used for
   `ailab`'s release-ppa.yml, or does a new Launchpad PPA + GPG key need
   to be set up for `automated-ken-runner` specifically? Not blocking
   for R1–R6 (only matters once we get to actually cutting a release).

## Also Worth Doing (adjacent, smaller, not blocking this plan)

The investigation for this plan turned up that **screenshot ingestion
for GitHub-Actions-sourced runs is currently dead code** — the deployed
workflow only uploads an Actions artifact, and nothing in
`snap_dashboard/` downloads artifacts. `ScreenshotReviewerAgent`'s
vision-comparison feature is therefore not actually seeing real
screenshots today for CI-sourced runs (only for the hypothetical
PR-based flow the template still describes). Once `test_run_screenshots`
exists (Phase R1), a small standalone follow-up — teach
`sync_test_runs()`/`poll_for_gh_run_id()` to download the
`yarf-results-*` artifact zip via the Actions API and populate the same
table — would fix that gap too, independent of the remote-runner work,
and using the exact same downstream table.

## Implementation Order

R1 → R2 → R3 → R4 → R5 are meant to be built and demoed in that order
against a single test machine before touching R6 (safety hardening) or
R7 (packaging polish / multi-runner UX). R1 has no user-visible effect
and is safe to land immediately; R2–R5 together are the minimum useful
end-to-end slice ("one machine, one runner, tests actually run and
report back").
