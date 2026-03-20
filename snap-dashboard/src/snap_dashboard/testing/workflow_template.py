"""GitHub Actions workflow YAML template for YARF snap testing."""

from __future__ import annotations

WORKFLOW_YAML: str = r"""\
# .github/workflows/snap-test.yml
#
# YARF snap testing workflow for use with snap-dashboard.
#
# Copy this file into your testing repository at:
#   .github/workflows/snap-test.yml
#
# Your testing repository should contain YARF test suites at:
#   suites/<snap_name>/suite/   (with __init__.robot and individual test files)
#
# Required repository secrets:
#   SNAP_DASHBOARD_GITHUB_TOKEN  - A GitHub token with `repo` scope so the
#                                  workflow can create branches, commits, and PRs.
#
# Optional secrets:
#   SNAPCRAFT_STORE_CREDENTIALS  - If you need store-authenticated snap installs.

name: YARF Snap Test

on:
  workflow_dispatch:
    inputs:
      snap_name:
        description: "Snap name to test"
        required: true
        type: string
      from_channel:
        description: "Channel to install from (candidate/edge)"
        required: true
        default: candidate
        type: string
      architecture:
        description: "Architecture to test (amd64, arm64)"
        required: true
        default: amd64
        type: string
      version:
        description: "Expected snap version (informational)"
        required: false
        type: string
      revision:
        description: "Expected snap revision (informational)"
        required: false
        type: string
      dashboard_run_id:
        description: "snap-dashboard TestRun ID (echoed into PR metadata)"
        required: false
        type: string

jobs:
  yarf-test:
    name: "YARF — ${{ inputs.snap_name }} ${{ inputs.architecture }} from ${{ inputs.from_channel }}"
    runs-on: ${{ inputs.architecture == 'arm64' && 'ubuntu-24.04-arm' || 'ubuntu-latest' }}
    permissions:
      contents: write
      pull-requests: write

    steps:
      - name: Checkout testing repo
        uses: actions/checkout@v4

      - name: Install snaps
        run: |
          sudo snap install --beta yarf
          sudo snap install mir-test-tools
          sudo snap install "${{ inputs.snap_name }}" --channel "${{ inputs.from_channel }}"
          sudo snap connect yarf:process-control

      - name: Run YARF tests
        id: yarf
        continue-on-error: true
        run: |
          mkdir -p "results/${{ inputs.snap_name }}"
          yarf \\
            --platform Mir \\
            --outdir "results/${{ inputs.snap_name }}" \\
            "suites/${{ inputs.snap_name }}/suite" || true
          echo "yarf_exit=$?" >> "$GITHUB_OUTPUT"

      - name: Upload test artifacts
        uses: actions/upload-artifact@v4
        with:
          name: "yarf-results-${{ inputs.snap_name }}-${{ github.run_id }}"
          path: "results/${{ inputs.snap_name }}"
          if-no-files-found: warn

      - name: Determine test status
        id: status
        run: |
          EXIT="${{ steps.yarf.outputs.yarf_exit }}"
          if [ "$EXIT" = "0" ]; then
            echo "status=passed" >> "$GITHUB_OUTPUT"
          else
            echo "status=failed" >> "$GITHUB_OUTPUT"
          fi

      - name: Commit results to branch
        env:
          GITHUB_TOKEN: ${{ secrets.SNAP_DASHBOARD_GITHUB_TOKEN || github.token }}
        run: |
          BRANCH="test-results/${{ inputs.snap_name }}/${{ github.run_id }}"
          git config user.name  "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git checkout -b "$BRANCH"
          mkdir -p "results/${{ inputs.snap_name }}"
          cat > "results/${{ inputs.snap_name }}/summary.txt" <<EOF
          snap: ${{ inputs.snap_name }}
          version: ${{ inputs.version }}
          channel: ${{ inputs.from_channel }}
          revision: ${{ inputs.revision }}
          status: ${{ steps.status.outputs.status }}
          gh_run: ${{ github.run_id }}
          EOF
          git add "results/${{ inputs.snap_name }}"
          git commit -m \\
            "test results: ${{ inputs.snap_name }} ${{ inputs.version }} (${{ steps.status.outputs.status }})"
          git push origin "$BRANCH"
          echo "BRANCH=$BRANCH" >> "$GITHUB_ENV"

      - name: Create (or update) test PR
        env:
          GH_TOKEN: ${{ secrets.SNAP_DASHBOARD_GITHUB_TOKEN || github.token }}
          GH_REPO: ${{ github.repository }}
        run: |
          STATUS="${{ steps.status.outputs.status }}"
          SNAP="${{ inputs.snap_name }}"
          VERSION="${{ inputs.version }}"
          CHANNEL="${{ inputs.from_channel }}"
          REVISION="${{ inputs.revision }}"
          RUN_ID="${{ inputs.dashboard_run_id }}"
          GH_RUN="${{ github.run_id }}"
          EXIT="${{ steps.yarf.outputs.yarf_exit }}"
          ARCH="${{ inputs.architecture }}"

          if [ "$STATUS" = "passed" ]; then
            TITLE_EMOJI="✅"
          else
            TITLE_EMOJI="❌"
          fi

          # Build screenshot section from PNGs committed to the branch
          SCREENSHOTS=""
          while IFS= read -r -d '' png; do
            rel="${png#./}"
            url="https://raw.githubusercontent.com/${GH_REPO}/${BRANCH}/${rel}"
            SCREENSHOTS="${SCREENSHOTS}
![$(basename "$png")]($url)
"
          done < <(find "results/${SNAP}" -name "*.png" -print0 2>/dev/null | sort -z)

          BODY=$(cat <<EOF
## YARF Test Results: ${SNAP}

| Field       | Value            |
|-------------|------------------|
| Snap         | \`${SNAP}\`      |
| Version      | \`${VERSION}\`   |
| Channel      | \`${CHANNEL}\`   |
| Architecture | \`${ARCH}\`      |
| Revision     | \`${REVISION}\`  |
| Status       | **${STATUS}**    |
| GHA Run      | [#${GH_RUN}](https://github.com/${GH_REPO}/actions/runs/${GH_RUN}) |
${SCREENSHOTS:+
## Screenshots

${SCREENSHOTS}}
<!-- snap-test-metadata
snap: ${SNAP}
version: ${VERSION}
from_channel: ${CHANNEL}
architecture: ${ARCH}
revision: ${REVISION}
dashboard_run_id: ${RUN_ID}
yarf_exit_code: ${EXIT}
gh_run_id: ${GH_RUN}
status: ${STATUS}
-->
EOF
          )

          gh label create "snap-test-results" --color "0075ca" --description "YARF snap test results" --force
          gh pr create \\
            --base main \\
            --head "$BRANCH" \\
            --title "${TITLE_EMOJI} ${SNAP} ${VERSION} (${CHANNEL}) — ${STATUS}" \\
            --body "$BODY" \\
            --label "snap-test-results"
"""
