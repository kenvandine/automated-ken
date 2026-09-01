# Getting Started with Automated Ken

A step-by-step guide to setting up the dashboard server and, once
enrolled, running automated snap tests against real runner machines.

This guide has two parts:
- **[Part 1: The dashboard server](#part-1-the-dashboard-server)** — works today, in either dev mode (below) or as a snap (see [`README.md`](README.md#snap-installation)).
- **[Part 2: Runner machines](#part-2-runner-machines)** — describes the private remote test runner. **Not implemented yet** — this documents the intended install/enroll flow from [`REMOTE_RUNNER_PLAN.md`](REMOTE_RUNNER_PLAN.md) so it's ready to go the moment Phases R1–R7 land; nothing in Part 2 will work until then.

---

# Part 1: The dashboard server

## 1. Clone and install

```bash
cd ~/src/github/kenvandine
git clone git@github.com:kenvandine/automated-ken.git
cd automated-ken/snap-dashboard

python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## 2. Create a GitHub OAuth App

snap-dashboard uses GitHub OAuth for authentication. You need an OAuth App registered at **GitHub → Settings → Developer settings → OAuth Apps → New OAuth App**.

| Field | Value |
|-------|-------|
| Application name | `snap-dashboard (dev)` |
| Homepage URL | `http://127.0.0.1:9080` |
| Authorization callback URL | `http://127.0.0.1:9080/auth/callback` |

Copy the **Client ID** and generate a **Client Secret**.

## 3. Create a GitHub Personal Access Token

You need a PAT with these scopes for full functionality:
- **`repo`** — read issues/PRs, create branches and files, dispatch workflows
- **`actions`** — trigger and monitor GitHub Actions runs

Create one at: **GitHub → Settings → Developer settings → Personal access tokens**.

## 4. Start the server

```bash
export GITHUB_CLIENT_ID=your_client_id
export GITHUB_CLIENT_SECRET=your_client_secret

snap-dashboard serve --port 9080
```

Or put them in `~/.local/share/snap-dashboard/config.env`:

```
GITHUB_CLIENT_ID=your_client_id
GITHUB_CLIENT_SECRET=your_client_secret
PORT=9080
```

The dashboard is now at **http://127.0.0.1:9080**.

## 5. First-run onboarding

Open the dashboard and click **Sign in with GitHub**. After authenticating, the onboarding wizard asks for:

1. **Publisher name** — your Snap Store publisher account (e.g. `ken-vandine`). A verification step confirms the account exists and shows how many snaps were found.
2. **GitHub token** — the PAT from step 3. Used for fetching issues/PRs and dispatching workflows.
3. **Testing repo** — the `owner/repo` of your YARF test repository (e.g. `kenvandine/automated-ken-tests`).

After completing onboarding, the initial data collection runs automatically in the background.

## 6. Configure agent settings (optional)

Go to **Settings → Agents & AI** to configure:

- **Bot GitHub token** — a PAT for a secondary bot account used to open version bump PRs. If not set, the primary token is used instead.
- **Lemonade server URL** — URL of a local [lemonade-server](https://github.com/amd/lemonade) instance for LLM vision analysis of screenshots.
- **Release scan interval** — how often the release scanner checks packaging repos (default: 4 hours).
- **Auto-merge** — automatically merge agent-approved version bump PRs.
- **Auto-rebuild stale snaps** — trigger rebuilds for snaps not published in N days (see below).

## 7. Set up the YARF testing repository

Clone your testing repo:

```bash
cd ~/src/github/kenvandine
git clone git@github.com:kenvandine/automated-ken-tests.git
```

The repo should contain YARF test suites at `suites/<snap_name>/suite/` and the YARF workflow at `.github/workflows/snap-test.yml`. snap-dashboard provides the workflow template — see **Settings → Docs** in the web UI for the latest version.

The repo structure:

```
automated-ken-tests/
├── .github/workflows/snap-test.yml
└── suites/
    ├── lemonade/suite/
    │   ├── __init__.robot
    │   └── test_lemonade.robot
    └── <snap-name>/suite/
        ├── __init__.robot
        └── test_<snap>.robot
```

Required secret in the testing repo:
- `SNAP_DASHBOARD_GITHUB_TOKEN` — a token with `repo` and `pull-requests: write` scope

## 8. Trigger a YARF test

1. Open **http://127.0.0.1:9080/testing**
2. The page shows snaps with versions in candidate/edge that differ from stable
3. Click **Trigger** next to a snap with a test suite in the testing repo
4. snap-dashboard dispatches the GitHub Actions workflow and starts polling for results

After the workflow completes, test status updates automatically. If the test passed, click **Promote** to release to stable.

## 9. Enable auto-testing (optional)

In **Settings**, toggle **Auto-test** on. snap-dashboard will automatically trigger YARF tests whenever a new version is detected in candidate or edge during a collection run.

## 10. Set up automated stale rebuilds (optional)

To automatically rebuild snaps that haven't published a new revision recently:

1. Generate a Snapcraft credentials file:
   ```bash
   snapcraft export-login --snaps '*' --channels candidate --acls package_upload creds.txt
   ```

2. Set `SNAPCRAFT_STORE_CREDENTIALS` in every snap packaging repo using the bundled helper:
   ```bash
   cd ~/src/github/kenvandine/automated-ken
   ./set-snapcraft-secret.sh creds.txt
   ```

3. In **Settings**, enable **Auto-rebuild stale snaps** and set the **Staleness window** (default: 30 days).

When triggered, the agent creates `.github/workflows/automated-snap-build.yml` in each packaging repo (if absent) and dispatches a build that publishes to the `candidate` channel.

---

# Part 2: Runner machines

> **Not implemented yet.** This section documents the intended
> install/enroll flow for the private remote test runner described in
> [`REMOTE_RUNNER_PLAN.md`](REMOTE_RUNNER_PLAN.md) (Phases R1–R7).
> Commands below (`automated-ken-runner ...`, the `/runners` page) don't
> exist in the dashboard yet — treat this as a preview of what setting
> up a fleet of runner laptops will look like once it ships, so this
> doc doesn't need to be rewritten from scratch at that point.

A runner is a physical desktop or laptop, sitting idle with its screen
unlocked, registered against your dashboard as a private test-execution
resource — similar to installing a self-hosted GitHub Actions runner.
Instead of a faked headless compositor, YARF tests run in the machine's
real logged-in desktop session, and screenshots are captured from the
real screen.

You'll typically want **more than one** runner registered: different
architectures, different desktop environments, or just enough capacity
that one busy/offline machine doesn't block testing. Each runner handles
one job at a time; the dashboard picks any idle, unlocked runner that
matches (or a specific one you choose on the `/testing` page), and falls
back to GitHub Actions automatically if none are available.

## 11. Pick a dedicated machine

Any spare desktop or laptop works, but **use a machine dedicated to this
purpose** — Part 2's setup disables screen-lock and auto-suspend
entirely (step 13), which means anyone with physical access to the
machine has full access to whatever's logged into it. Don't do this on
your daily-driver laptop.

Requirements:
- Ubuntu (or another distro with `systemd-logind` and, ideally, GNOME —
  see [`REMOTE_RUNNER_PLAN.md`](REMOTE_RUNNER_PLAN.md#open-questions-need-a-decision-beforewhile-implementing) open question #2 on desktop-environment scope)
- `snapd` installed (used to install/run the snaps under test)
- Logged into a real graphical desktop session (not just SSH'd in — YARF
  needs an actual compositor to render into)
- Plugged into power, or otherwise not going to run out of battery
  mid-fleet

## 12. Install `automated-ken-runner`

```bash
sudo add-apt-repository ppa:ken-vandine/automated-ken-runner
sudo apt update
sudo apt install automated-ken-runner
```

## 13. Enroll the machine

On the dashboard, go to **Runners → Add runner** to generate a one-time
enrollment token (expires in ~15 minutes), then on the runner machine:

```bash
automated-ken-runner enroll --server https://your-dashboard-host:9080 --token <token>
```

This also runs the machine-preparation step automatically, which:
- disables the screen lock and idle-suspend timeouts
- keeps the display powered on (so screenshots don't capture a blanked screen)
- disables lid-close suspend, if it's a laptop

You'll see a warning printed to confirm you understand the tradeoff:

```
⚠️  This machine's screen will no longer lock or auto-suspend. Only do
this on a dedicated test device with no sensitive data logged in, in a
physically secured location — anyone with physical access now has full
access to whatever's logged in.
```

If you'd rather manage those power settings yourself (e.g. via a `dconf`
policy across a fleet), pass `--skip-power-settings` and set them up on
your own — see the exact `gsettings` commands in
[`REMOTE_RUNNER_PLAN.md`](REMOTE_RUNNER_PLAN.md#runner-machine-preparation-disabling-screen-lock-and-auto-suspend).

## 14. Start the runner service

```bash
systemctl --user enable --now automated-ken-runner
```

It's a `systemd --user` service (not system-wide) — it needs the real
logged-in graphical session's display and D-Bus session to launch GUI
apps into, so it has to run as your user, in your session, not as root.

## 15. Verify it shows up

Back on the dashboard's **Runners** page, the machine should appear
within a few seconds, reporting `idle` (unlocked, no recent input). If
it shows `offline`, check `systemctl --user status automated-ken-runner`
and `journalctl --user -u automated-ken-runner -f` on the runner machine.

## 16. Add more runners

Repeat steps 11–15 on each additional machine — a fresh enrollment token
per machine, generated from the same **Runners** page. There's no limit
on how many you register; more idle runners just means more test
capacity and less waiting on GitHub Actions.

## 17. Send a test to a specific runner (or "any available")

On the **Testing** page, the "Run on" control next to each snap lets you
pick **GitHub Actions**, a specific runner by name, or **Any available
runner**. Triggering a test with a runner selected dispatches it the
same way as today's GitHub Actions flow, except results (including
screenshots captured on the real screen) come back from that machine
directly.

---

## Quick reference

| Action | URL |
|--------|-----|
| Dashboard | http://127.0.0.1:9080 |
| Testing page | http://127.0.0.1:9080/testing |
| Agent activity | http://127.0.0.1:9080/agents |
| Version bump PRs | http://127.0.0.1:9080/version-bumps |
| Settings | http://127.0.0.1:9080/settings |
| Runners *(planned)* | http://127.0.0.1:9080/runners |
