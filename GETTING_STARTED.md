# Getting Started with automated-ken

A step-by-step guide to setting up automated-ken and running tests from the automated-ken-tests repository.

---

## 1. Set up the snap-dashboard (development mode)

```bash
cd ~/src/github/kenvandine/automated-ken/snap-dashboard
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## 2. Create a GitHub Personal Access Token

You need a token with these scopes:
- **`repo`** — to read issues/PRs and dispatch workflows
- **`actions`** — to trigger and monitor GitHub Actions runs

Create one at: **GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens** (or classic).

## 3. Configure snap-dashboard

Create the config file:

```bash
mkdir -p ~/.local/share/snap-dashboard
cat > ~/.local/share/snap-dashboard/config.env << 'EOF'
PUBLISHER=ken-vandine
GITHUB_TOKEN=ghp_YOUR_TOKEN_HERE
TESTING_REPO=kenvandine/automated-ken-tests
AUTO_TEST=false
PORT=9080
EOF
```

Key settings for testing:
- **`TESTING_REPO`** — points to `kenvandine/automated-ken-tests` (the `owner/repo` format)
- **`AUTO_TEST`** — set to `true` later if you want tests triggered automatically; keep `false` to start with manual triggers

## 4. Run the initial data collection

```bash
snap-dashboard collect
```

This populates the SQLite database (`~/.local/share/snap-dashboard/snap-dashboard.db`) with your snaps, their channel versions, and issue/PR data from GitHub/GitLab.

## 5. Start the web server

```bash
snap-dashboard serve
```

The dashboard is now at **http://127.0.0.1:9080**.

## 6. Set up the automated-ken-tests repository

Clone it (if you haven't already):

```bash
cd ~/src/github/kenvandine
git clone git@github.com:kenvandine/automated-ken-tests.git
```

The repo structure looks like:

```
automated-ken-tests/
├── .github/workflows/snap-test.yml   # GitHub Actions workflow
└── suites/
    ├── ask-ubuntu/suite/
    │   ├── __init__.robot
    │   └── test_askubuntu.robot
    └── lemonade/suite/
        ├── __init__.robot
        └── test_lemonade.robot
```

## 7. Install the GitHub Actions workflow

The workflow file at `.github/workflows/snap-test.yml` should already exist (generated from snap-dashboard's built-in template). Ensure it's pushed to the `main` branch of automated-ken-tests so GitHub Actions can find it.

The workflow is triggered via `workflow_dispatch` — snap-dashboard calls the GitHub API to start it.

## 8. Write a test suite for a snap

To add tests for a new snap (e.g., `my-snap`), create:

```
automated-ken-tests/suites/my-snap/suite/__init__.robot
automated-ken-tests/suites/my-snap/suite/test_mysnap.robot
```

Use the existing suites as reference. A minimal `__init__.robot`:

```robot
*** Settings ***
Library    Process
Suite Setup    Start Xvfb
Suite Teardown    Stop Xvfb

*** Keywords ***
Start Xvfb
    Start Process    Xvfb    :99    -screen    0    1280x800x24
    Set Environment Variable    DISPLAY    :99

Stop Xvfb
    Terminate All Processes
```

And a minimal test file:

```robot
*** Settings ***
Library    Process
Library    OperatingSystem

*** Test Cases ***
Snap Is Installed
    ${result}=    Run Process    snap    list    my-snap
    Should Be Equal As Integers    ${result.rc}    0
```

Push the new suite to the tests repo.

## 9. Trigger a test from the dashboard

1. Open **http://127.0.0.1:9080/testing**
2. The page shows snaps with versions in candidate/beta/edge that differ from stable
3. Click **Trigger** next to a snap that has a test suite in the tests repo
4. This dispatches the GitHub Actions workflow with the snap name, channel, version, and revision

## 10. Sync test results

After the workflow runs:

1. Click **Sync** on the testing page (or POST `/testing/sync`)
2. snap-dashboard polls GitHub for open PRs labeled with test results
3. Test status updates: pending → running → passed/failed
4. If passed, you can click **Promote** to run `snapcraft release <snap> <revision> stable`

## 11. (Optional) Enable auto-testing

Once you're comfortable with the workflow:

```bash
# In config.env
AUTO_TEST=true
```

Or via snap config:

```bash
snap set snap-dashboard auto-test=true
```

This makes snap-dashboard automatically trigger tests when it detects new versions in non-stable channels during collection.

## 12. (Optional) Run tests locally

For local debugging without GitHub Actions:

```bash
pip install canonical-yarf
Xvfb :99 -screen 0 1280x800x24 &
export DISPLAY=:99
cd ~/src/github/kenvandine/automated-ken-tests
yarf --platform Mir suites/ask-ubuntu/suite/
```

---

## Quick Reference

| Action | Command / URL |
|--------|--------------|
| Collect data | `snap-dashboard collect` |
| Start server | `snap-dashboard serve` |
| Dashboard | http://127.0.0.1:9080 |
| Testing page | http://127.0.0.1:9080/testing |
| Trigger test | POST `/testing/trigger/{snap_name}` |
| Sync results | POST `/testing/sync` |
| Add a snap | `snap-dashboard add <name> --packaging-repo URL` |

The key requirement for a snap to be testable is that it has a matching suite directory in `automated-ken-tests/suites/<snap_name>/suite/` with an `__init__.robot` file. The orchestrator checks for this before allowing a test to be triggered.
