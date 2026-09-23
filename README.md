# snap-dashboard

An agentic snap maintenance platform for snap publishers. Monitors channel versions, automates version bump PRs, triggers YARF tests, rebuilds stale snaps, and presents everything in a live web dashboard — all running as a self-hosted snap.

## Features

### Monitoring
- **Channel comparison** — `stable`, `candidate`, `beta`, and `edge` side-by-side for every snap, colour-coded so promotable updates stand out
- **Attention-needed highlights** — snaps where a newer revision is waiting in candidate/edge are surfaced at the top of the dashboard
- **Issue & PR tracking** — open issues and pull requests fetched from GitHub and GitLab packaging repos, shown per snap
- **Auto-discovery** — on first run, all snaps published by the configured publisher account are discovered via the Snap Store API

### Agents
A pool of background agents runs continuously and reports live to the dashboard:

| Agent | What it does |
|-------|-------------|
| **Collector** | Refreshes Snap Store channel maps and GitHub/GitLab issue/PR counts on a per-user interval, so the dashboard stays current without manual refreshes |
| **Release Scanner** | Checks packaging repos for new upstream releases; spawns a Version Bumper when found |
| **Version Bumper** | Opens a version bump PR on the packaging repo via a bot GitHub account (no git clone — uses GitHub Contents API) |
| **PR Monitor** | Polls open version bump PRs; triggers YARF tests when build CI passes |
| **Screenshot Reviewer** | Uses a local Lemonade LLM (vision model) to compare before/after screenshots and approve or reject the PR |
| **Stale Build Scanner** | Finds snaps with no new publication in N days; creates `automated-snap-build.yml` in the packaging repo if absent and dispatches a rebuild to the `candidate` channel |

### YARF Testing
- Trigger YARF snap tests against a GitHub Actions–based testing repository
- Test results are polled from the Actions run status and reflected on the dashboard
- Passed tests can be promoted to stable from the dashboard
- **Planned:** a private remote test runner lets you register idle desktop/laptop machines as test-execution resources — see [`REMOTE_RUNNER_PLAN.md`](REMOTE_RUNNER_PLAN.md) (not implemented yet)

## Quick start (development)

```sh
git clone https://github.com/kenvandine/automated-ken
cd automated-ken/snap-dashboard

python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Set a GitHub OAuth app client ID/secret in the environment
export GITHUB_CLIENT_ID=...
export GITHUB_CLIENT_SECRET=...

snap-dashboard serve          # http://127.0.0.1:9080
```

Open `http://127.0.0.1:9080` and log in with your GitHub account. The first-run onboarding wizard guides you through configuring your publisher name, GitHub token, and (optionally) bot account details.

## Snap installation

This snap isn't tied to any particular publisher account — anyone can
install it and point it at their own snaps.

```sh
sudo snap install snap-dashboard
```

Create a GitHub OAuth App (**GitHub → Settings → Developer settings → OAuth
Apps → New OAuth App**) with callback URL `http://<host>:9080/auth/callback`,
then configure it and start the server:

```sh
snap set snap-dashboard github-client-id=...
snap set snap-dashboard github-client-secret=...
```

The `serve` daemon starts (and restarts itself to pick up the new config)
automatically. Open `http://127.0.0.1:9080`, sign in with GitHub, and the
onboarding wizard walks you through the rest (Snap Store publisher account,
personal access token, etc.) — all stored per-account in the local database,
so multiple people can run their own independent setups against the same
snap-dashboard instance if they want to.

A session-signing secret is generated automatically on first run and
persisted (`snap get snap-dashboard session-secret`), so logins survive
daemon restarts without any extra configuration. To bind to a non-default
address/port:

```sh
snap set snap-dashboard bind=0.0.0.0 port=8080
```

## Configuration

All per-user settings are managed through the **Settings** page in the web UI. The available settings are:

| Setting | Purpose |
|---------|---------|
| Publisher | Snap Store publisher account for auto-discovery |
| GitHub token | Primary PAT — read issues/PRs, dispatch workflows |
| Bot GitHub token | Secondary PAT for a bot account that opens version bump PRs |
| Bot GitHub login | Display name of the bot account |
| Testing repo | `owner/repo` of the YARF test repository |
| Auto-test | Automatically trigger YARF tests when new versions land in candidate/edge |
| Lemonade server URL | Base URL of a local lemonade-server for LLM vision analysis |
| Lemonade model | Vision-capable model name (e.g. `llava`) |
| Release scan interval | How often the release scanner runs (1–24 h) |
| Auto-merge | Automatically merge agent-approved version bump PRs |
| Auto-rebuild stale snaps | Dispatch rebuilds for snaps with no recent publication |
| Staleness window | How many days without a publish before a snap is considered stale (default 30) |

## Web dashboard

### Pages

| Path | Description |
|------|-------------|
| `/` | Summary dashboard — channel comparison + attention-needed cards |
| `/snap/<name>` | Snap detail — full channel map, issue/PR list, edit repo URLs |
| `/snaps/add` | Add a snap — Store search, auto-populate repo URLs |
| `/testing` | YARF test runs — trigger, monitor, promote to stable |
| `/agents` | Live agent activity feed |
| `/version-bumps` | Version bump PRs grouped by status (approved / needs review / rejected) |
| `/version-bumps/<id>` | PR detail — screenshots, agent reasoning, merge/reject actions |
| `/settings` | Per-user configuration |
| `/onboarding` | First-run wizard |
| `/docs` | In-app workflow documentation |

## Automated snap builds (stale rebuild)

When **Auto-rebuild stale snaps** is enabled, the Stale Build Scanner agent:

1. Identifies snaps where the most recent channel map publication is older than the configured staleness window.
2. Creates `.github/workflows/automated-snap-build.yml` in the snap's packaging repo if it doesn't exist (via GitHub Contents API — no clone needed).
3. Dispatches a `workflow_dispatch` event to trigger the workflow, which builds and publishes to the `candidate` channel.
4. Records the trigger to avoid re-firing within the same window.

The build workflow requires a `SNAPCRAFT_STORE_CREDENTIALS` secret in each packaging repo. Use the bundled helper script to set it across all repos at once:

```sh
snapcraft export-login --snaps '*' --channels candidate --acls package_upload creds.txt
./set-snapcraft-secret.sh creds.txt
```

> **Note:** GitHub personal accounts don't support account-level secrets. Either add the secret to each repo individually (the script handles this), or create a GitHub Organisation and use org-level secrets.

## Project structure

```
snap-dashboard/
├── src/snap_dashboard/
│   ├── cli.py                   Click CLI entry point
│   ├── collector.py             Snap Store + GitHub data collection pipeline
│   ├── config.py                Config loader (env → config.env → defaults)
│   ├── auth.py                  GitHub OAuth + session helpers
│   ├── agents/
│   │   ├── base.py              BaseAgent abstract class
│   │   ├── runner.py            Thread pool + periodic scheduler
│   │   ├── scheduling.py        Per-user agent scheduling (single source of truth)
│   │   ├── collector_agent.py   Periodic Store/GitHub data refresh
│   │   ├── release_scanner.py   Upstream release detection
│   │   ├── version_bumper.py    Version bump PR creation
│   │   ├── pr_monitor.py        CI status polling + YARF trigger
│   │   ├── screenshot_reviewer.py  LLM vision comparison
│   │   └── stale_build_scanner.py  Stale snap rebuild trigger
│   ├── db/
│   │   ├── models.py            SQLAlchemy ORM models
│   │   └── session.py           Session factory + init_db() + migrations
│   ├── github/
│   │   ├── client.py            Issues/PR fetching (GitHub + GitLab)
│   │   ├── utils.py             Repo URL/slug parsing helpers
│   │   ├── bot_client.py        Bot account: branches, files, PRs, workflow dispatch
│   │   └── pr_viewer.py         Test result PR parsing
│   ├── lemonade/
│   │   └── client.py            OpenAI-compatible client for lemonade-server
│   ├── snapcraft/
│   │   ├── fetcher.py           Fetch snapcraft.yaml via GitHub Contents API
│   │   ├── parser.py            Parse snapcraft.yaml source parts
│   │   ├── upstream.py          Latest-version detection (GitHub/PyPI/GitLab)
│   │   └── build_workflow_template.py  automated-snap-build.yml template
│   ├── store/
│   │   └── client.py            Snap Store API v2 client
│   ├── testing/
│   │   ├── orchestrator.py      YARF workflow dispatch + status polling
│   │   ├── promoter.py          Snap Store channel promotion
│   │   └── workflow_template.py snap-test.yml YARF workflow template
│   └── web/
│       ├── app.py               FastAPI application + agent startup
│       ├── routes/              Route handlers
│       ├── templates/           Jinja2 HTML templates
│       └── static/              CSS + JS assets
├── set-snapcraft-secret.sh      Helper: set SNAPCRAFT_STORE_CREDENTIALS across all repos
├── snap/
│   ├── snapcraft.yaml           Snap package definition
│   └── hooks/configure          snap set handler
└── bin/snap-dashboard           Wrapper script for the snap
```

## Data storage

All data is stored locally in SQLite:
- **Snap (runtime):** `$SNAP_DATA/snap-dashboard.db`
- **Dev mode:** `~/.local/share/snap-dashboard/snap-dashboard.db`

No data is sent to any third party. The tool only reads from:
- `https://api.snapcraft.io` — public snap metadata and channel maps
- `https://api.github.com` — repository data (token optional but recommended)
- `https://gitlab.com/api/v4` — GitLab repository data (optional)
- Local lemonade-server — LLM vision analysis (optional, self-hosted)

## Building the snap

```sh
cd snap-dashboard
snapcraft
sudo snap install snap-dashboard_*.snap --dangerous
```

## Dependencies

| Package | Use |
|---------|-----|
| `fastapi` | Web framework |
| `uvicorn` | ASGI server |
| `sqlalchemy` | ORM / SQLite |
| `click` | CLI framework |
| `httpx` | HTTP client (Store, GitHub, Lemonade) |
| `jinja2` | HTML templating |
| `python-multipart` | Form parsing |
| `pyyaml` | snapcraft.yaml parsing |

## License

MIT
