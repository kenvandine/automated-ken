# Automated Ken

A self-hosted, agentic snap maintenance platform for snap publishers. It
watches your snaps' channels and upstream projects, opens version-bump PRs,
tests new versions on real amd64 and arm64 machines, reviews the
screenshots with a local vision model, and promotes each version to
`stable` for all architectures at once.

It has two parts:

| Component | What it is | Where it runs |
|-----------|------------|---------------|
| **`automated-ken`** (`snap-dashboard/`) | The web dashboard and the background agents | One server (strict snap) |
| **`automated-ken-runner`** (`automated-ken-runner/`) | A test-execution agent that runs tests in a real desktop session | One or more dedicated test machines (classic snap) |

New here? Follow [`GETTING_STARTED.md`](GETTING_STARTED.md), or
[`QUICKSTART.md`](QUICKSTART.md) for the condensed version. The dashboard
also has built-in help at `/docs`.

## How it works

```
upstream release ──► Release Scanner ──► Version Bumper ──► bump PR on packaging repo
                                                               │
                                                  PR Monitor waits for CI to pass
                                                               │
                              one test job per architecture ◄──┘
                              (amd64 runner, arm64 runner)
                                                               │
                        Screenshot Reviewer compares each arch against its stable baseline
                                                               │
                                        verdict for the whole set ──► you merge (or auto-merge)

new candidate version ──► (auto-)test amd64 + arm64 on runners ──► vision review per arch
                                                               │
                       all architectures approved ──► promote the whole set to stable
```

- **Runners are matched by architecture.** Each test job targets one
  architecture and is only picked up by a runner of that architecture
  belonging to the same user. Only **amd64** and **arm64** are tested; other
  architectures a snap ships (armhf, riscv64, …) are skipped.
- **Releases are promoted as a set.** A candidate version's amd64 and arm64
  revisions go to `stable` together, only once every architecture has passed
  and (for auto-promotion) been approved by the vision review. You can
  override a missing architecture manually, but the override has to be
  confirmed and is recorded.

## Features

### Monitoring
- **Channel map** — `stable`, `candidate`, `beta` and `edge` per architecture for every snap
- **Attention-needed highlights** — snaps with a newer version waiting in candidate/edge
- **Issues & PRs** — open issues and PRs from the packaging and upstream repos (GitHub and GitLab)
- **Auto-discovery** — on first run, every snap published by your Store account is added

### Agents

Background agents run on a schedule and report live on the **Agents** page.

| Agent | What it does | When |
|-------|-------------|------|
| **Collector** | Refreshes Store channel maps and issue/PR data; with *automatic testing* on, queues tests for new candidate/edge versions | Every *collection interval* (default 6 h) |
| **Release Scanner** | Checks each packaging repo's `snapcraft.yaml` parts for newer upstream releases | Every *release scan interval* (default 4 h), or **Scan Now** |
| **Version Bumper** | Opens a version-bump PR from the bot account (GitHub Contents API, no clone) | When the scanner finds a release |
| **PR Monitor** | Advances bump PRs: waits for CI, queues one runner test per architecture, hands results to the reviewer, auto-merges if enabled, and notices PRs merged/closed on GitHub | Every 5 min |
| **Screenshot Reviewer** | Compares each architecture's screenshots with its stable baseline using the local vision model; gives the bump one verdict for the whole set | After a bump's tests finish |
| **Candidate Reviewer / Auto-promoter** | Reviews candidate test runs the same way; with auto-promote on, promotes the release set when every architecture is approved | After a candidate run passes |
| **Runner Watchdog** | Fails runner jobs that exceed the job timeout and frees the runner | Every 2 min |
| **Stale Build Scanner** | Rebuilds snaps with no new publication in N days (adds `automated-snap-build.yml` if needed, publishes to `candidate`) | Daily, if enabled |
| **Upstream Maintainer** | For upstream repos you own: dependency-update PRs, review requests, issue triage (via the coding backend) | Daily, if enabled |
| **Fleet Normalization** | One-off pass that opens a PR per packaging repo to drop legacy `sync-release`-style workflows, normalise the build/publish workflow and add an `AGENTS.md` (via the coding backend) | On demand, if enabled |
| **Snapcraft Credential Sync** | Pushes your Store credential into every packaging repo's Actions secrets | On demand |

Tasks that need real code changes (fixing CI on a bump PR, upstream
maintenance, fleet normalization) are delegated to a **coding backend** —
GitHub Copilot cloud agent by default. Every dispatched task is listed on the
**Copilot Tasks** page.

### Testing on real hardware
- Runners install the snap under test from the Store (`snap install/refresh --channel=…`) and run it in a real logged-in desktop session.
- If the packaging repo has a YARF suite at `tests/suite/__init__.robot` the runner uses it; otherwise it runs a launch-and-screenshot smoke test.
- A runner only takes jobs while its desktop has been idle for 2 minutes and isn't locked, so it never interferes with someone using the machine.
- Queue, priorities, cancellation and per-runner status are on the **Runners** page.

### Local AI
A private Lemonade server is
bundled and started automatically (no setup), used for screenshot review
and PR descriptions. You can point it at an existing Lemonade server
instead in **Settings → Agents & AI**. Without a model, reviews fall back to
"needs manual review" rather than approving anything.

## Installation

```sh
sudo snap install automated-ken
```

Create a GitHub OAuth App (**GitHub → Settings → Developer settings → OAuth
Apps → New OAuth App**) with callback URL `http://<host>:9080/auth/callback`,
then:

```sh
sudo snap set automated-ken github-client-id=... github-client-secret=...
```

The `serve` daemon restarts itself with the new config. Open
`http://127.0.0.1:9080`, sign in with GitHub (the first account to sign in
becomes the admin), and the onboarding wizard asks for your Store publisher
name and a GitHub token.

Optional snap settings:

| Key | Default | Purpose |
|-----|---------|---------|
| `bind` | `127.0.0.1` | Listen address (use `0.0.0.0` so runners on other machines can reach it) |
| `port` | `9080` | Listen port |
| `session-secret` | generated | Cookie-signing secret; generated and persisted on first run |

Then set up at least one runner — see
[`GETTING_STARTED.md`](GETTING_STARTED.md#part-2-runner-machines).

## Configuration

Everything else is per user, on the **Settings** page:

| Section | Settings |
|---------|----------|
| Publisher | Snap Store publisher account; collection interval |
| GitHub Token | Personal access token with `repo` and `workflow` scopes — reading repos and issues/PRs, merging bump PRs, creating/dispatching build workflows, and Copilot tasks |
| Snapcraft Store Credential | A `snapcraft export-login` credential — used to promote to `stable`, and synced into packaging repos for rebuilds |
| Testing | Automatic testing of new candidate/edge versions; runner job timeout |
| Agents & AI | Lemonade backend (embedded or system) and model override; bot account login/token for bump PRs; release scan interval; auto-merge; auto-promote and its confidence threshold; stale rebuilds and staleness window; coding backend and the delegated-task toggles |

## Pages

| Path | Description |
|------|-------------|
| `/` | Dashboard — channel comparison and attention-needed cards |
| `/snap/<name>` | Snap detail — channel map, issues/PRs, repo URLs |
| `/snaps/add` | Add a snap manually |
| `/testing` | Needs-testing list (one row per version, all architectures), pending release sets, run history |
| `/testing/runs/<id>` | One run — screenshots, AI review, and its release set |
| `/runners` | Enroll/revoke runners; job queue with priorities and cancel |
| `/version-bumps` | Version-bump PRs grouped by status |
| `/version-bumps/<id>` | One bump — per-architecture results and screenshots, promote/merge/reject |
| `/agents` | Live agent status and activity feed |
| `/copilot-tasks` | Delegated coding tasks and their PRs |
| `/stats` | Model usage stats |
| `/settings` | Per-user configuration |
| `/admin` | Allowlist and admin users (admins only) |
| `/docs` | Built-in help |

## Data and network access

All state lives in SQLite at `$SNAP_COMMON/snap-dashboard.db` (snap) or
`~/.local/share/snap-dashboard/snap-dashboard.db` (dev). The server talks to:

- `api.snapcraft.io` / `dashboard.snapcraft.io` — channel maps, and releasing revisions to `stable`
- `api.github.com` — repos, issues/PRs, branches/PRs from the bot account, Actions secrets, Copilot agent tasks
- `gitlab.com` — issues/PRs for GitLab-hosted repos
- The bundled Lemonade server (localhost) — or your own, if configured
- Your runners, which poll the server over HTTP(S); runners never accept inbound connections

## Development

```sh
git clone https://github.com/kenvandine/automated-ken
cd automated-ken/snap-dashboard
python3 -m venv .venv && . .venv/bin/activate
pip install -e . pytest anyio
export GITHUB_CLIENT_ID=... GITHUB_CLIENT_SECRET=...
snap-dashboard serve          # http://127.0.0.1:9080
python -m pytest              # test suite
```

Build the snaps with `snapcraft pack` in `snap-dashboard/` and
`automated-ken-runner/`. See [`snap-dashboard/README.md`](snap-dashboard/README.md)
and [`automated-ken-runner/README.md`](automated-ken-runner/README.md) for
component details.

## Design documents

[`SPEC.md`](SPEC.md), [`AGENTIC_PLAN.md`](AGENTIC_PLAN.md) and
[`REMOTE_RUNNER_PLAN.md`](REMOTE_RUNNER_PLAN.md) record the original designs.
Each has a status note at the top describing how the shipped system differs.

## License

The server (`snap-dashboard/`) is GPL-3.0-or-later and the runner
(`automated-ken-runner/`) is MIT, as declared in each `pyproject.toml`.
