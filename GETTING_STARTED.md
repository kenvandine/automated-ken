# Getting Started with Automated Ken

A step-by-step guide to setting up the dashboard server, enrolling runner
machines, and getting new snap versions tested and promoted.

- **[Part 1: The dashboard server](#part-1-the-dashboard-server)**
- **[Part 2: Runner machines](#part-2-runner-machines)**
- **[Part 3: Testing and promoting](#part-3-testing-and-promoting)**
- **[Part 4: Optional automation](#part-4-optional-automation)**

---

# Part 1: The dashboard server

## 1. Install

As a snap (recommended):

```bash
sudo snap install automated-ken
```

Or from source, for development:

```bash
git clone https://github.com/kenvandine/automated-ken.git
cd automated-ken/snap-dashboard
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
```

## 2. Create a GitHub OAuth App

Login is GitHub OAuth. Register an app at **GitHub → Settings → Developer
settings → OAuth Apps → New OAuth App**:

| Field | Value |
|-------|-------|
| Application name | `Automated Ken` |
| Homepage URL | `http://<host>:9080` |
| Authorization callback URL | `http://<host>:9080/auth/callback` |

Copy the **Client ID** and generate a **Client Secret**.

## 3. Configure and start the server

Snap:

```bash
sudo snap set automated-ken github-client-id=<id> github-client-secret=<secret>
# Runners on other machines need to reach the server:
sudo snap set automated-ken bind=0.0.0.0
```

The `serve` daemon restarts itself whenever you `snap set`. A session
secret is generated automatically on first run.

From source, put the same values in `~/.local/share/snap-dashboard/config.env`
(or export them) and start the server:

```
GITHUB_CLIENT_ID=<id>
GITHUB_CLIENT_SECRET=<secret>
BIND=0.0.0.0
PORT=9080
```

```bash
snap-dashboard serve
```

The dashboard is at **http://&lt;host&gt;:9080**.

## 4. Sign in and complete onboarding

Click **Sign in with GitHub**. The first account to sign in becomes the
administrator; everyone after that must be added to the allowlist on the
**Admin** page first.

The onboarding wizard asks for:

1. **Snap Store publisher** — your Store account name (e.g. `ken-vandine`). It checks the account and shows how many snaps it found; they're added automatically.
2. **GitHub token** *(optional here, needed for most features)* — see the next step.

When you finish, the first data collection starts in the background and
the agents are scheduled.

## 5. Add a GitHub token

Create a personal access token at **GitHub → Settings → Developer settings →
Personal access tokens** with the **`repo`** and **`workflow`** scopes, and
paste it on **Settings → GitHub Token** (or in onboarding).

It's used to read packaging repos and their issues/PRs, merge bump PRs,
create and dispatch build workflows, set Actions secrets, and create
Copilot agent tasks. Test jobs also use it to download a snap's YARF suite
from its packaging repo.

## 6. Add the Snap Store credential

Promoting to `stable` (and stale rebuilds) need a Store credential:

```bash
snapcraft export-login -
```

Paste the output on **Settings → Snapcraft Store Credential**. The
dashboard uses it to release revisions through the Store API, and **Sync to
All Packaging Repos Now** pushes it into every packaging repo as the
`SNAPCRAFT_STORE_CREDENTIALS` Actions secret.

## 7. Set up the bot account (for version-bump PRs)

Version bumps are opened from a separate GitHub account so they're easy to
tell apart. Create one, give it write access to your packaging repos, and
enter its login and a `repo`-scoped token under **Settings → Agents & AI**.
Without a bot token, the Version Bumper skips opening PRs.

---

# Part 2: Runner machines

A runner is a physical desktop or laptop, registered against your dashboard
like a self-hosted CI runner. Tests run in its real logged-in desktop
session and screenshots come from the real screen. Runners poll the
dashboard over HTTP(S), so they work behind NAT with no inbound ports.

Each runner reports its architecture and only picks up jobs for that
architecture. **Enroll at least one amd64 and one arm64 runner** — a job for
an architecture with no runner just waits in the queue, and a release set
isn't promoted automatically until every architecture has passed.

## 8. Pick a dedicated machine

Use a machine dedicated to testing. Preparing it turns on autologin and
turns off the screen lock and blanking, so anyone with physical access has
full access to whatever is logged in.

Requirements:
- Ubuntu with GNOME (idle detection uses `loginctl`, screenshots use a bundled GNOME Shell extension)
- Logged into a real graphical session (not just SSH)
- On power

## 9. Install the runner

```bash
sudo snap install automated-ken-runner --classic
```

It uses classic confinement because it has to install arbitrary snaps,
launch GUI apps in your session, and capture the screen.

Jobs install the snap under test with `sudo snap install/refresh` from a
background service, so `sudo` must not prompt for `snap`:

```bash
echo "$USER ALL=(root) NOPASSWD: /usr/bin/snap" | sudo tee /etc/sudoers.d/automated-ken-runner
```

## 10. Prepare the machine

```bash
automated-ken-runner prepare-machine
```

This installs YARF, checks the `sudo` rule above, installs and enables the
screenshot extension, enables autologin, and disables screen lock/blanking.
It reboots if the desktop setup changed (pass `--no-reboot` to skip). It's
safe to re-run.

## 11. Enroll it

On the dashboard, open **Runners → + Enroll a runner**, give it a name, and
copy the command it shows (the token is one-time and expires in 15
minutes). On the runner:

```bash
automated-ken-runner enroll --server http://<dashboard-host>:9080 --token <token>
systemctl --user enable --now snap.automated-ken-runner.run.service
```

The service runs as your user, inside your graphical session.

## 12. Check it shows up

The **Runners** page should list the machine within a few seconds with its
architecture (e.g. `arm64`). Status is:

| Status | Meaning |
|--------|---------|
| `idle` | Ready — desktop idle for 2+ minutes and unlocked |
| `busy` | Running a job, or someone is using the machine |
| `locked` | Screen locked — won't take jobs |
| `offline` | No heartbeat for 90 seconds |

If it's `offline`, check `systemctl --user status snap.automated-ken-runner.run.service`
and `journalctl --user -u snap.automated-ken-runner.run.service -f`.
`automated-ken-runner status` shows what the runner thinks its idle state is.

Repeat steps 8–12 for each machine.

---

# Part 3: Testing and promoting

## 13. What gets tested

Every test job is one snap version on one architecture, run on a runner of
that architecture:

- The runner installs the snap from the job's channel (`candidate` or `edge`).
- If the packaging repo has a YARF suite at `tests/suite/__init__.robot`, it runs that suite.
- Otherwise it launches the app, waits for it to render, and takes a screenshot (a smoke test).
- Screenshots and the full log are uploaded to the dashboard.

Only **amd64** and **arm64** are tested. Other architectures a snap ships
are ignored.

## 14. Run tests

On the **Testing** page, **Needs testing** lists every snap whose
candidate or edge version is newer than stable — one row per version, with
all its architectures together. **Run Tests (amd64, arm64)** queues one job
per architecture. Follow them on the same page or on **Runners**, where
you can reorder or cancel queued and running jobs.

A job that runs longer than the **runner job timeout** (Settings →
Testing, default 10 minutes, counted from when a runner picks it up) is
marked failed and its runner freed.

## 15. Review

When a candidate run passes, the local vision model compares its
screenshots with the last stable screenshots for the same architecture (or,
the first time, just checks the app visibly launched) and records
*approve*, *reject* or *needs review* with a confidence score. Open a run
from the Testing page to see its screenshots side by side, the verdict, and
the rest of its release set.

## 16. Promote a release set

A candidate version is promoted to `stable` **for all its architectures at
once**:

- **Pending Promotion** on the Testing page (and a run's detail page) shows each release set and the state of every architecture.
- When every architecture has passed, **🚀 Promote set to Stable** releases them together.
- If an architecture hasn't passed (not tested, still running, failed…), the button becomes **⚠ Promote (override)**. Its confirmation names the architectures that will be left out, the server refuses a partial promotion unless that override was confirmed, and each promoted run records which architectures were skipped.

With **Auto-promote** on (Settings → Agents & AI), this happens
automatically once every architecture passes *and* is approved at or above
the confidence threshold. If any architecture is missing or not approved,
nothing is auto-promoted.

## 17. Version-bump PRs

When the Release Scanner finds a newer upstream release, the Version Bumper
opens a PR on the packaging repo from the bot account. The PR Monitor then:

1. waits for the PR's CI to pass (re-checking if a later push fixes a failure),
2. queues one test per architecture from `edge`,
3. has the Screenshot Reviewer check each architecture and gives the PR one verdict — a regression on any architecture rejects the whole set,
4. merges it if **Auto-merge** is on and the verdict is *approve*.

On **Version Bumps**, open a PR to see each architecture's result and
screenshots, then **Merge**, **Reject**, **Re-run YARF** (re-tests every
architecture; only the newest run per architecture counts), or **Promote**.
Promote works like a release set: all architectures, or a confirmed
override that is recorded as *partially promoted*.

Bumps merged or closed directly on GitHub are picked up automatically.

---

# Part 4: Optional automation

All under **Settings**:

- **Automatic testing** (Testing) — after every channel-map refresh, queue tests for any candidate or edge version that hasn't been tested yet.
- **Auto-merge** / **Auto-promote** (Agents & AI) — see steps 16 and 17.
- **Auto-rebuild stale snaps** (Agents & AI) — for snaps with no new publication within the staleness window (default 30 days), add `.github/workflows/automated-snap-build.yml` to the packaging repo if it's missing and dispatch a build that publishes to `candidate`. Needs the Store credential synced (step 6).
- **Delegated coding tasks** (Agents & AI) — auto-fix failing CI on bump PRs, maintain upstream repos you own, and a one-off fleet normalization. These go to the **coding backend** (GitHub Copilot cloud agent by default) and open PRs for you to review; track them on **Copilot Tasks**.
- **Lemonade** (Agents & AI) — the bundled server is used by default. Choose *System Lemonade* to use your own server, or override the model.

---

## Quick reference

| Page | Path |
|------|------|
| Dashboard | `/` |
| Testing | `/testing` |
| Runners | `/runners` |
| Version bumps | `/version-bumps` |
| Agent activity | `/agents` |
| Copilot tasks | `/copilot-tasks` |
| Settings | `/settings` |
| Admin | `/admin` |
| Help | `/docs` |

| Runner command | Purpose |
|----------------|---------|
| `automated-ken-runner prepare-machine` | Install dependencies and configure the desktop |
| `automated-ken-runner enroll --server URL --token TOKEN` | Register with a dashboard |
| `automated-ken-runner status` | Show enrollment and idle state |
| `automated-ken-runner unenroll` | Forget the stored credentials |
| `automated-ken-runner run` | Run the job loop in the foreground (the service does this) |
