# snap-dashboard (the `automated-ken` snap)

The Automated Ken server: a FastAPI web dashboard plus the background
agents that monitor snaps, open version-bump PRs, queue runner tests and
promote release sets. For what the system does and how to set it up, see
the [top-level README](../README.md) and
[`GETTING_STARTED.md`](../GETTING_STARTED.md). This file covers running and
developing the server itself.

## Requirements

- Python 3.11+
- A GitHub OAuth App (for login)

## Running from source

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
snap-dashboard serve            # http://127.0.0.1:9080
```

## Server configuration

Read from environment variables first, then from `config.env` —
`$SNAP_COMMON/config.env` in the snap (written by the `configure` hook from
`snap set automated-ken …`), otherwise `~/.local/share/snap-dashboard/config.env`.

| Key | snap option | Default | Description |
|-----|-------------|---------|-------------|
| `GITHUB_CLIENT_ID` | `github-client-id` | — | OAuth App client ID (required) |
| `GITHUB_CLIENT_SECRET` | `github-client-secret` | — | OAuth App client secret (required) |
| `SESSION_SECRET` | `session-secret` | generated | Cookie-signing secret; generated and persisted on first run if unset |
| `BIND` | `bind` | `127.0.0.1` | Listen address |
| `PORT` | `port` | `9080` | Listen port |
| `LEMONADE_EMBEDDED_PORT` | — | `13411` | Port of the bundled Lemonade server |
| `GITHUB_TOKEN`, `PUBLISHER`, `COLLECT_INTERVAL_HOURS` | `github-token`, `publisher`, `interval` | — | Fallbacks for the `collect`/`add` CLI commands only |

Everything else is per user and set in the web UI (**Settings**).

## CLI

| Command | Purpose |
|---------|---------|
| `snap-dashboard serve [--bind ADDR] [--port N]` | Run the web server and agents |
| `snap-dashboard collect` | Run one data collection using the CLI fallback settings |
| `snap-dashboard add NAME [--packaging-repo URL] [--upstream-repo URL] [--notes TEXT]` | Track a snap |
| `snap-dashboard remove NAME` | Stop tracking a snap |
| `snap-dashboard list` | List tracked snaps |

In the snap, the `serve` app is a daemon and the CLI is `automated-ken.snap-dashboard`.

## Users and access

The first GitHub account to sign in becomes the administrator. Later logins
must be on the allowlist (**Admin** page; logins are matched
case-insensitively). Every user has their own snaps, runners, test runs,
settings and agent activity.

## Tests

```bash
pip install pytest anyio
python -m pytest
```

## Project structure

```
src/snap_dashboard/
├── cli.py                    Click CLI (serve, collect, add, remove, list)
├── config.py                 Server config (env → config.env → defaults)
├── auth.py                   Session auth + per-user settings view
├── collector.py              Snap Store + GitHub/GitLab data collection
├── agents/
│   ├── base.py / runner.py   BaseAgent, thread pool, scheduler, live activity tracker
│   ├── scheduling.py         Per-user periodic agent schedule
│   ├── collector_agent.py    Periodic collection (+ automatic test queuing)
│   ├── release_scanner.py    Upstream release detection
│   ├── version_bumper.py     Version-bump PRs from the bot account
│   ├── pr_monitor.py         Bump PR state machine: CI → runner tests → review → merge
│   ├── screenshot_reviewer.py  Vision review of a bump's per-arch runs; one verdict per set
│   ├── stable_promoter.py    Promotes a bump's architectures to stable together
│   ├── test_run_auto_promoter.py  Reviews candidate runs; auto-promotes complete sets
│   ├── runner_watchdog.py    Fails runner jobs that exceed the job timeout
│   ├── stale_build_scanner.py  Rebuilds snaps with no recent publication
│   ├── upstream_maintainer.py, repo_normalizer.py, coding_backend.py
│   │                         Delegated coding tasks (Copilot cloud agent by default)
│   └── snapcraft_credential_sync.py  Pushes the Store credential to repo secrets
├── db/                       SQLAlchemy models; session + additive migrations
├── github/                   GitHub/GitLab clients, bot client, Copilot agent API, secrets sync
├── lemonade/                 Lemonade client, bundled server manager, task→model defaults
├── runners/                  Runner token helpers and online/offline status
├── snapcraft/                snapcraft.yaml fetch/parse, upstream version checks, build workflow template
├── store/                    Snap Store API client
├── testing/
│   ├── orchestrator.py       What needs testing, testable architectures, job queuing
│   ├── release_set.py        Candidate release sets: readiness and set promotion
│   ├── baselines.py          Stable screenshot baselines
│   ├── promoter.py           Store release API, PR merge
│   └── suite_zip.py          Packages a repo's YARF suite for runners
└── web/
    ├── app.py                FastAPI app, startup scheduling
    ├── routes/               Pages and APIs (runner_api.py is the runner protocol)
    ├── templates/            Jinja2 templates
    └── static/               CSS
```

## Runner API

Runners authenticate with a bearer secret issued at enrollment
(`web/routes/runner_api.py`):

| Endpoint | Purpose |
|----------|---------|
| `POST /api/runners/enroll` | Exchange a one-time token for a runner secret (reports name and architecture) |
| `PATCH /api/runners/{id}/heartbeat` | Idle/lock state and architecture; response carries `cancel_requested` |
| `GET /api/runners/{id}/next-job` | Long-poll for a job of this runner's user and architecture |
| `GET /api/runners/{id}/jobs/{job}/suite` | Zip of the snap's YARF suite (404 → smoke test) |
| `PATCH /api/runners/{id}/jobs/{job}` | Report status (`running`, `passed`, `failed`, `cancelled`, …) and log |
| `POST /api/runners/{id}/jobs/{job}/screenshots` | Upload a screenshot |

## Data storage

SQLite at `$SNAP_COMMON/snap-dashboard.db` in the snap (shared across
revisions, so refreshes don't duplicate it), or
`~/.local/share/snap-dashboard/snap-dashboard.db` from source. Schema
changes are additive `ALTER TABLE` migrations applied at startup
(`db/session.py`).

## License

GPL-3.0-or-later — see [`LICENSE`](../LICENSE).
