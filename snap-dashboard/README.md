# snap-dashboard

A self-hosted web dashboard for managing Ubuntu snap packages. It tracks channel versions, open issues and pull requests across your published snaps, and integrates with [YARF](https://snapcraft.io/yarf) for automated snap testing and promotion.

## Features

- **Auto-discovery** — finds all snaps for a given Snap Store publisher
- **Channel tracking** — shows stable / candidate / beta / edge versions per architecture
- **Issue & PR tracking** — fetches open GitHub issues and PRs from packaging and upstream repos
- **YARF testing** — triggers GitHub Actions workflows to run YARF test suites against snaps, then shows results inline
- **One-click promotion** — promotes a tested snap revision to stable via `snapcraft release`
- **Multi-tenant** — GitHub OAuth login; first user becomes admin and manages an access allowlist
- **Per-user isolation** — each user has their own set of snaps, test runs, and configuration

## Requirements

- Python 3.11+
- A Snap Store publisher account
- A GitHub account (for OAuth login)
- A GitHub OAuth App (for multi-user login)
- Optional: a [YARF testing repository](#yarf-testing-setup)

## Installation

```bash
git clone https://github.com/kenvandine/automated-ken
cd automated-ken/snap-dashboard
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Configuration

Configuration is read from environment variables first, then from `~/.local/share/snap-dashboard/config.env` (or `$SNAP_DATA/config.env` when running as a snap).

### Server-level settings (config.env or environment)

| Key | Required | Description |
|-----|----------|-------------|
| `SESSION_SECRET` | Yes | Random secret for signing session cookies. Generate with `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `GITHUB_CLIENT_ID` | Yes | GitHub OAuth App client ID |
| `GITHUB_CLIENT_SECRET` | Yes | GitHub OAuth App client secret |
| `BIND` | No | Listen address (default: `127.0.0.1`) |
| `PORT` | No | Listen port (default: `9080`) |

Example `~/.local/share/snap-dashboard/config.env`:

```
SESSION_SECRET=your-random-secret-here
GITHUB_CLIENT_ID=Ov23liXXXXXXXXXXXXXX
GITHUB_CLIENT_SECRET=your_client_secret_here
```

### Per-user settings

All per-user settings (publisher, GitHub token, testing repo, collection interval) are configured through the **Settings** page in the web UI after logging in.

## GitHub OAuth App setup

1. Go to **GitHub → Settings → Developer settings → OAuth Apps → New OAuth App**
2. Set **Application name**: `snap-dashboard`
3. Set **Homepage URL**: `http://localhost:9080` (or your public URL)
4. Set **Authorization callback URL**: `http://localhost:9080/auth/callback`
5. Click **Register application**
6. Copy the **Client ID** and generate a **Client Secret**
7. Add both to your `config.env`

## Running

```bash
snap-dashboard serve
```

The dashboard will be available at `http://localhost:9080`.

On first startup:
1. Visit `http://localhost:9080` — you will be redirected to the login page
2. Sign in with GitHub; you become the administrator automatically
3. Complete the onboarding wizard (publisher name, GitHub token)
4. A first collection run discovers your snaps

## First-user admin

The **first GitHub account to log in** becomes the administrator. As admin you can:
- Add other GitHub accounts to the access allowlist (via `/admin`)
- Toggle admin status for other users

All subsequent logins require the user's GitHub account to be on the allowlist.

## YARF Testing Setup

YARF testing requires a separate **testing repository** on GitHub that contains your test suites and a GitHub Actions workflow.

### 1. Create the testing repository

Create a new GitHub repository (e.g. `you/snap-tests`).

### 2. Add the workflow

Download the workflow template from **Settings → Download Workflow Template** (or the Testing page when no repo is configured) and commit it to `.github/workflows/snap-test.yml` in your testing repository.

### 3. Add a repository secret

In your testing repository, go to **Settings → Secrets and variables → Actions** and add:

- `SNAP_DASHBOARD_GITHUB_TOKEN` — a GitHub Personal Access Token with **repo** scope. This allows the workflow to push result branches and create PRs.

### 4. Create test suites

Organise YARF suites under:

```
suites/<snap_name>/suite/
├── __init__.robot      # Suite setup/teardown (variables, Xvfb, etc.)
└── test_<name>.robot   # Test cases
```

### 5. Configure snap-dashboard

In **Settings**, set **Testing Repository** to `owner/repo` (e.g. `kenvandine/snap-tests`).

### Testing workflow

1. snap-dashboard detects a snap with a newer version in **candidate** or **edge** than **stable**
2. You click **Run Tests** (or enable auto-test)
3. snap-dashboard dispatches a `workflow_dispatch` event to the testing repository
4. The workflow installs YARF, the snap under test, and runs the suite
5. Results (including screenshots) are committed to a branch and a PR is opened
6. snap-dashboard polls GitHub and updates the run status live
7. Once tests pass, you can **Promote to Stable** directly from the dashboard

## YARF suite structure

```robot
# suites/mysnap/suite/__init__.robot
*** Settings ***
Suite Setup     Start Virtual Display
Suite Teardown  Stop Virtual Display

*** Keywords ***
Start Virtual Display
    # Xvfb is started by the CI workflow; set DISPLAY if needed
    Set Environment Variable    DISPLAY    :99

Stop Virtual Display
    Pass
```

```robot
# suites/mysnap/suite/test_mysnap.robot
*** Settings ***
Library    Collections
Library    OperatingSystem

*** Test Cases ***
Snap Is Installed
    ${rc}=    Run And Return RC    snap list mysnap
    Should Be Equal As Integers    ${rc}    0

App Launches
    ${handle}=    Start Process    mysnap
    Sleep    2s
    Process Should Be Running    ${handle}
    Terminate Process    ${handle}
```

## Project structure

```
snap-dashboard/
├── src/snap_dashboard/
│   ├── auth.py               # Session auth helpers
│   ├── collector.py          # Snap Store + GitHub data fetcher
│   ├── config.py             # Server-level configuration
│   ├── db/
│   │   ├── models.py         # SQLAlchemy ORM models
│   │   └── session.py        # DB engine, migrations
│   ├── github/
│   │   ├── client.py         # GitHub issues/PR client
│   │   └── pr_viewer.py      # Test PR parsing
│   ├── store/
│   │   └── client.py         # Snap Store API client
│   ├── testing/
│   │   ├── orchestrator.py   # Workflow dispatch + status polling
│   │   ├── promoter.py       # snapcraft release + PR closing
│   │   └── workflow_template.py  # Embeds snap-test.yml template
│   └── web/
│       ├── app.py            # FastAPI app + middleware
│       └── routes/           # auth, admin, dashboard, snaps, settings, testing
├── pyproject.toml
└── README.md
```

## Data storage

All data is stored in a SQLite database at `~/.local/share/snap-dashboard/snap-dashboard.db` (or `$SNAP_DATA/snap-dashboard.db` when running as a snap).

## License

GPL-3.0-or-later
