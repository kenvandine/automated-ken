# Getting Started with snap-dashboard

A step-by-step guide to setting up snap-dashboard in development mode and running automated snap tests.

---

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

## Quick reference

| Action | URL |
|--------|-----|
| Dashboard | http://127.0.0.1:9080 |
| Testing page | http://127.0.0.1:9080/testing |
| Agent activity | http://127.0.0.1:9080/agents |
| Version bump PRs | http://127.0.0.1:9080/version-bumps |
| Settings | http://127.0.0.1:9080/settings |
