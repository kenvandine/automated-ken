#!/usr/bin/env bash
# set-snapcraft-secret.sh — add SNAPCRAFT_STORE_CREDENTIALS to every GitHub repo
# that contains a snap packaging file.
#
# Usage:
#   ./set-snapcraft-secret.sh <creds-file>
#
# The creds file is the output of:
#   snapcraft export-login --snaps '*' --channels edge --acls package_upload creds.txt
#
# Requires: gh (GitHub CLI), authenticated with the account that owns the repos.

set -euo pipefail

usage() {
    echo "Usage: $0 <creds-file>" >&2
    exit 1
}

[[ $# -eq 1 ]] || usage
CREDS_FILE="$1"

[[ -f "$CREDS_FILE" ]] || { echo "Error: file not found: $CREDS_FILE" >&2; exit 1; }
[[ -s "$CREDS_FILE" ]] || { echo "Error: creds file is empty: $CREDS_FILE" >&2; exit 1; }

command -v gh &>/dev/null || { echo "Error: gh CLI not found — install from https://cli.github.com" >&2; exit 1; }

gh auth status &>/dev/null || { echo "Error: not logged in — run 'gh auth login' first" >&2; exit 1; }

CREDS="$(cat "$CREDS_FILE")"

echo "Fetching repo list..."
mapfile -t REPOS < <(gh repo list --limit 1000 --json nameWithOwner -q '.[].nameWithOwner')

if [[ ${#REPOS[@]} -eq 0 ]]; then
    echo "No repositories found." >&2
    exit 1
fi

echo "Found ${#REPOS[@]} repo(s). Checking for snap packaging files..."

SNAP_YAML_PATHS=(
    "snap/snapcraft.yaml"
    "snapcraft.yaml"
    ".snapcraft.yaml"
)

set_secret() {
    local repo="$1"
    if gh secret set SNAPCRAFT_STORE_CREDENTIALS \
            --body "$CREDS" \
            --repo "$repo" 2>/dev/null; then
        echo "  [ok]     $repo"
    else
        echo "  [failed] $repo"
    fi
}

ok=0
failed=0
skipped=0

for repo in "${REPOS[@]}"; do
    is_snap_repo=false
    for path in "${SNAP_YAML_PATHS[@]}"; do
        if gh api "repos/${repo}/contents/${path}" &>/dev/null; then
            is_snap_repo=true
            break
        fi
    done

    if $is_snap_repo; then
        if gh secret set SNAPCRAFT_STORE_CREDENTIALS \
                --body "$CREDS" \
                --repo "$repo" 2>/dev/null; then
            echo "  [ok]     $repo"
            (( ok++ )) || true
        else
            echo "  [failed] $repo"
            (( failed++ )) || true
        fi
    else
        echo "  [skip]   $repo  (no snapcraft.yaml)"
        (( skipped++ )) || true
    fi
done

echo ""
echo "Done: ${ok} set, ${failed} failed, ${skipped} skipped (no snap packaging file)."
