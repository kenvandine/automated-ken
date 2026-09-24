"""GitHub Actions workflow YAML template for automated snap builds.

This workflow is created by snap-dashboard in packaging repos that have gone
stale. It always publishes to the candidate channel.

Required repository secret:
  SNAPCRAFT_STORE_CREDENTIALS — export of a snapcraft login with package_upload
  permission for the candidate channel.

  Generate with:
    snapcraft export-login --snaps <snap-name> \\
      --channels candidate --acls package_upload creds.txt

  Note: GitHub personal accounts do not support account-level secrets, so
  this still needs to land as a per-repo secret — but you don't have to add
  it by hand anymore. Paste one credential covering your whole fleet into
  Settings → "Snapcraft Store Credential" and use "Sync to All Packaging
  Repos Now"; the dashboard pushes it out to every tracked repo's Actions
  secrets automatically (see agents/snapcraft_credential_sync.py).
"""

from __future__ import annotations

WORKFLOW_YAML: str = r"""
# .github/workflows/automated-snap-build.yml
#
# Automated snap build workflow managed by snap-dashboard.
# Builds the snap and publishes it to the candidate channel.
#
# Required repository secret (auto-provisioned by the dashboard's Settings
# page — see "Snapcraft Store Credential" — no manual setup needed):
#   SNAPCRAFT_STORE_CREDENTIALS

name: Automated Snap Build

on:
  workflow_dispatch:
    inputs:
      dashboard_trigger_id:
        description: "snap-dashboard StaleBuildTrigger ID (informational)"
        required: false
        type: string

jobs:
  build:
    name: "Build and publish to candidate"
    runs-on: ubuntu-latest

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Build snap
        uses: snapcore/action-build@v1
        id: build

      - name: Publish snap to candidate
        uses: snapcore/action-publish@v1
        env:
          SNAPCRAFT_STORE_CREDENTIALS: ${{ secrets.SNAPCRAFT_STORE_CREDENTIALS }}
        with:
          snap: ${{ steps.build.outputs.snap }}
          release: candidate
"""

WORKFLOW_PATH = ".github/workflows/automated-snap-build.yml"
WORKFLOW_FILENAME = "automated-snap-build.yml"
