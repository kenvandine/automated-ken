# Agentic Snap Maintenance System — Implementation Plan

## Overview

This document describes the planned evolution of snap-dashboard from a monitoring
dashboard into an agent-driven maintenance platform. A pool of background agents
continuously scans for upstream releases, creates packaging PRs, triggers CI,
analyzes results with a local LLM, and presents decisions for human review. The
web UI becomes a live command center showing every agent's work in real time.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Web Dashboard (FastAPI + SSE)             │
│   Agent Feed │ Version Bump PRs │ Screenshot Review │ Decisions  │
└────────────────────────────┬────────────────────────────────────┘
                             │ read/write
┌────────────────────────────▼────────────────────────────────────┐
│                        SQLite Database                           │
│  agents  │ upstream_releases │ version_bump_prs │ screenshots    │
└───┬───────────┬──────────────────┬────────────────┬─────────────┘
    │           │                  │                │
┌───▼──┐  ┌────▼────┐  ┌──────────▼──┐  ┌─────────▼──────┐
│Rel.  │  │Version  │  │ PR Monitor  │  │Screenshot      │
│Scan  │  │Bumper   │  │   Agent     │  │Reviewer Agent  │
│Agent │  │ Agent   │  │             │  │(Lemonade LLM)  │
└───┬──┘  └────┬────┘  └──────┬──────┘  └─────────┬──────┘
    │ fetch    │ bot PR        │ poll GH             │ vision LLM
    ▼          ▼               ▼                     ▼
packaging   packaging      test results         lemonade-server
repos       repos          PRs on GH              (local)
```

## New Configuration Values

Added to `UserConfig` model and the settings page:

| Key | Purpose | Default |
|-----|---------|---------|
| `lemonade_server_url` | Base URL for lemonade-server | `http://localhost:8080` |
| `lemonade_model` | Model name to use (vision-capable) | `llava` |
| `bot_github_token` | PAT for the secondary bot GitHub account | — |
| `bot_github_login` | Login name of the bot account (display only) | — |
| `agent_interval_hours` | How often the release scan runs | `4` |
| `auto_merge` | Whether agent-approved PRs are auto-merged | `false` |

## New Database Models

### `agents` — Agent run history
```
id, agent_type (release_scanner|version_bumper|pr_monitor|screenshot_reviewer),
snap_name, status (idle|running|done|error), started_at, finished_at,
result_summary, error_msg, user_id
```

### `upstream_releases` — Discovered new upstream versions
```
id, snap_id, part_name, source_type (git|pypi|launchpad),
source_url, current_version, latest_version, release_url,
release_notes (text), discovered_at, acted_on (bool), acted_at
```

### `version_bump_prs` — PRs created by the bot account
```
id, snap_id, upstream_release_id, bot_pr_url, bot_pr_number,
packaging_repo, branch_name, old_version, new_version,
status (open|ci_pending|ci_passed|ci_failed|yarf_running|
        yarf_passed|yarf_failed|agent_approved|agent_rejected|
        needs_review|merged|closed),
test_run_id (FK → test_runs), created_at, updated_at, merged_at,
agent_decision (approve|reject|needs_review),
agent_confidence (float), agent_reasoning (text)
```

### `screenshot_comparisons` — LLM vision analysis results
```
id, version_bump_pr_id, test_run_id,
baseline_url, baseline_image_b64,
new_url, new_image_b64,
llm_prompt, llm_response (text),
decision (approve|reject|needs_review),
confidence (float), reasoning (text),
analyzed_at
```

---

## Phase 7: Agent Infrastructure + Lemonade Integration

**Goal:** Foundational plumbing — agent runner, LLM client, config.

### New files
- `snap_dashboard/agents/__init__.py`
- `snap_dashboard/agents/base.py` — `BaseAgent` abstract class with `run()`,
  status lifecycle, DB logging, lemonade client injection
- `snap_dashboard/agents/runner.py` — `AgentRunner`: thread pool, scheduling,
  per-user agent queues, `run_agent(agent_instance)` method
- `snap_dashboard/lemonade/client.py` — OpenAI-compatible HTTP client pointing
  at `lemonade_server_url`; `chat()` and `vision_compare()` methods; graceful
  fallback when server not available

### DB changes
- Add `agents` table
- Add new `UserConfig` columns: `lemonade_server_url`, `lemonade_model`,
  `bot_github_token`, `bot_github_login`, `agent_interval_hours`, `auto_merge`

### Settings page changes
New "Agents & AI" section with lemonade URL, model, and bot token fields.

### Agent runner lifecycle
```
AgentRunner (singleton, started at app startup)
  → schedule_periodic(ReleaseScannerAgent, interval=agent_interval_hours)
  → on demand: run_agent(VersionBumperAgent(snap, release))
  → on demand: run_agent(ScreenshotReviewerAgent(test_run))
  → continuous: PRMonitorAgent runs every 5 min for open version_bump_prs
```

---

## Phase 8: Release Scanner Agent

**Goal:** Scan every snap's packaging repo for new upstream versions.

### New files
- `snap_dashboard/agents/release_scanner.py` — `ReleaseScannerAgent`
- `snap_dashboard/snapcraft/parser.py` — `parse_snapcraft_yaml(content) → list[SourcePart]`;
  extracts `source`, `source-tag`, `source-type`, `source-branch`, version fields per part
- `snap_dashboard/snapcraft/fetcher.py` — `fetch_snapcraft_yaml(packaging_repo, token) → str | None`;
  fetches raw file from GitHub Contents API

### SourcePart detection by source type

| source-type | Detection | Latest version API |
|-------------|-----------|-------------------|
| `git` + github.com URL | `source-tag` is current | GitHub Releases + Tags API |
| `git` + launchpad.net URL | `source-tag` | Launchpad API |
| PyPI tarball URL | URL contains version | PyPI JSON API |
| `git` + gitlab.com | `source-tag` | GitLab Releases API |

### Flow
1. For each Snap with `packaging_repo` set, fetch `snapcraft.yaml`
2. Parse all parts that have a `source` field
3. For each part, call the appropriate upstream API to find the latest release
4. If latest > current: create/update `UpstreamRelease` record
5. Log run to `agents` table
6. Dashboard shows new "Upstream Updates Available" badge

---

## Phase 9: Version Bumper Agent

**Goal:** Use the bot account to open version bump PRs on packaging repos.

### New files
- `snap_dashboard/agents/version_bumper.py` — `VersionBumperAgent`
- `snap_dashboard/github/bot_client.py` — `BotGitHubClient`: wraps `bot_github_token`,
  provides `create_branch()`, `update_file()`, `create_pr()` using GitHub Contents API
  (no git clone required)

### Version bump strategy
1. Fetch current `snapcraft.yaml` content + SHA from GitHub Contents API
2. Patch `source-tag` (and `version:` if present) for the relevant part using
   regex/YAML edit
3. If lemonade is available, generate commit message and PR description from
   upstream release notes
4. Push changed file to branch `version-bump/<snap>/<new-version>` on packaging repo
5. Open PR from bot account: title `chore: update <part> to <new-version>`,
   body includes changelog excerpt + upstream release link
6. Create `VersionBumpPR` record with status `open`

**Safeguard:** Only one open version bump PR per snap+part at a time.

---

## Phase 10: CI Integration for Version Bump PRs

**Goal:** Version bump PRs get snap builds + YARF tests triggered automatically.

### Two-part approach

**Part A — Snap build CI in the packaging repo:**
Add a new workflow template (parallel to the existing `workflow_template.py`)
that builds the snap on PR and uploads the `.snap` artifact:
- `snap_dashboard/snapcraft/build_workflow_template.py`

**Part B — YARF trigger on CI pass:**
- `snap_dashboard/agents/pr_monitor.py` — `PRMonitorAgent` polls all
  `open`/`ci_*` `VersionBumpPR` records every 5 minutes
- When build CI passes: automatically call `trigger_workflow()` for the snap
- Link the resulting `TestRun` to the `VersionBumpPR` via `test_run_id`

### Status transitions
```
open → ci_pending → ci_passed → yarf_running → yarf_passed
                 → ci_failed
                                             → yarf_failed
```

---

## Phase 11: Screenshot Analysis Agent

**Goal:** LLM vision model compares before/after screenshots and recommends
merge or reject.

### New files
- `snap_dashboard/agents/screenshot_reviewer.py` — `ScreenshotReviewerAgent`

### Flow
1. Triggered when `VersionBumpPR.status` reaches `yarf_passed` or `yarf_failed`
2. Fetch YARF result PR screenshots (PNG files from test branch)
3. Fetch baseline screenshots from the most recent passing `TestRun` for that
   snap on `stable`
4. If lemonade-server is available with a vision-capable model:
   - Prompt: *"You are reviewing a snap package update. Compare these two
     screenshots: [BEFORE] and [AFTER]. The snap is `<name>`, updating from
     `<old>` to `<new>`. Does the application appear to function correctly?
     Are there any visual regressions, crashes, or error dialogs? Provide:
     decision (approve/reject/needs_review), confidence (0.0–1.0), and a
     one-paragraph reasoning."*
   - Parse structured response
5. Fallback (no lemonade): use YARF exit code + screenshot presence as
   heuristic; set confidence 0.5 and decision `needs_review`
6. Write `ScreenshotComparison` record; update `VersionBumpPR.agent_decision`

### Decision → status mapping
| YARF result | Agent decision | Final status |
|-------------|---------------|-------------|
| passed | approve | `agent_approved` |
| passed | needs_review | `needs_review` |
| passed | reject | `agent_rejected` |
| failed | any | `agent_rejected` |

---

## Phase 12: Live Dashboard Overhaul

**Goal:** The UI becomes a real-time agent command center.

### New routes
- `GET /agents` — Agent activity feed page (backed by SSE)
- `GET /version-bumps` — All version bump PRs grouped by status
- `GET /version-bumps/{id}` — Detail view with screenshots, agent reasoning,
  and action buttons
- `POST /version-bumps/{id}/merge` — Merge the PR via primary GitHub token
- `POST /version-bumps/{id}/reject` — Close the PR
- `POST /version-bumps/{id}/request-review` — Mark for human review
- `GET /api/events` — Server-Sent Events stream

### SSE event types
- `agent_status` — Agent started/finished/errored
- `version_bump_update` — Status change on a version bump PR
- `new_release` — New upstream release discovered

### Dashboard additions
- "Upstream Updates" summary card — counts of pending/approved/needs-review
- Per-snap row gains inline agent status badges
- "Agent Feed" sidebar showing last 20 agent actions

### Version Bumps page layout
```
┌─────────────────────────────────────────────────────┐
│ AGENT APPROVED (3)                    [Merge All ▼]  │
│  lemonade  v0.9→1.0  ✅ YARF passed  [Merge] [Skip] │
│  gnome-calc  v46→47  ✅ YARF passed  [Merge] [Skip] │
├─────────────────────────────────────────────────────┤
│ NEEDS YOUR REVIEW (2)                                │
│  firefox-snap  v132→133  ⚠ Agent unsure  [Review]   │
├─────────────────────────────────────────────────────┤
│ AGENT REJECTED (1)                                   │
│  thunderbird  v133→134  ❌ Visual regression  [View] │
└─────────────────────────────────────────────────────┘
```

### Review detail page
- Side-by-side screenshot comparison (before left, after right)
- Agent's reasoning paragraph
- Links to YARF PR, build CI run, upstream release notes
- Action buttons: **Merge**, **Reject**, **Re-run YARF**, **Ignore**

---

---

## Phase 13: Stale Snap Rebuild

**Goal:** Automatically rebuild snaps that have gone stale (no new publication in N days).

### New files
- `snap_dashboard/agents/stale_build_scanner.py` — `StaleSnapScannerAgent`
- `snap_dashboard/snapcraft/build_workflow_template.py` — `automated-snap-build.yml` template

### New DB model: `stale_build_triggers`
```
id, snap_id, user_id, packaging_repo, channel,
days_since_publish, status (triggered|skipped|failed),
error_msg, triggered_at
```

### New `UserConfig` columns
| Column | Default | Purpose |
|--------|---------|---------|
| `auto_rebuild_stale` | `False` | Enable/disable the agent per user |
| `stale_build_days` | `30` | Days without publication before a snap is considered stale |

### Flow
1. Agent runs every 24h per user (scheduled at startup alongside the release scanner)
2. For each snap with a GitHub `packaging_repo`:
   - Query `ChannelMap` for the most recent `released_at` across all channels/architectures
   - Skip if published within `stale_build_days`
   - Skip if a `StaleBuildTrigger` with `status=triggered` already exists within the window
3. For qualifying (stale) snaps:
   - Create `.github/workflows/automated-snap-build.yml` in the packaging repo via GitHub Contents API if not already present
   - Dispatch `workflow_dispatch` on `automated-snap-build.yml`; the workflow always publishes to the `candidate` channel
   - Record the outcome in `StaleBuildTrigger`

### Build workflow
`automated-snap-build.yml` is managed entirely by snap-dashboard:
- No channel input — always releases to `candidate`
- Triggered via `workflow_dispatch` with a `dashboard_trigger_id` input for correlation
- Requires `SNAPCRAFT_STORE_CREDENTIALS` secret in the packaging repo

### Helper script
`set-snapcraft-secret.sh <creds-file>` — iterates all GitHub repos owned by the authenticated user, identifies snap packaging repos, and sets `SNAPCRAFT_STORE_CREDENTIALS` in each one.

---

## Phase Summary

| Phase | Scope | Key New Files | DB Changes |
|-------|-------|--------------|------------|
| **7** | Agent infra + Lemonade client | `agents/base.py`, `agents/runner.py`, `lemonade/client.py` | `agent_runs` table; 6 new `user_config` columns |
| **8** | Release scanner | `agents/release_scanner.py`, `snapcraft/parser.py`, `snapcraft/fetcher.py` | `upstream_releases` table |
| **9** | Version bumper | `agents/version_bumper.py`, `github/bot_client.py` | `version_bump_prs` table |
| **10** | CI integration | `snapcraft/build_workflow_template.py`, `agents/pr_monitor.py` | `version_bump_prs.test_run_id` FK |
| **11** | Screenshot reviewer | `agents/screenshot_reviewer.py` | `screenshot_comparisons` table |
| **12** | Live dashboard | `routes/agents.py`, `routes/version_bumps.py`, `routes/api.py` | — |
| **13** | Stale snap rebuild | `agents/stale_build_scanner.py`, `snapcraft/build_workflow_template.py`, `set-snapcraft-secret.sh` | `stale_build_triggers` table; 2 new `user_config` columns |

---

## Key Design Decisions

**Lemonade-first, degrade gracefully.** Every agent checks if lemonade is
reachable before calling it. If not, agents still work — they use structured
heuristics and flag items for human review rather than making AI decisions.
Vision comparison is the only thing that truly needs lemonade; release detection
and PR creation are purely deterministic.

**No git clone — GitHub Contents API only.** The version bumper reads and
writes single files via the GitHub REST API. This keeps the snap's dependency
footprint minimal and avoids managing local checkouts.

**Bot account is config, not code.** `bot_github_token` and `bot_github_login`
live in `UserConfig` alongside the primary token. All PR creation uses the bot
token; all merges use the primary token.

**SSE over WebSocket.** Server-Sent Events are simpler, work with FastAPI's
`StreamingResponse`, and are one-directional (server → client), which is all
that is needed for live updates. No additional library required.

**Existing `TestRun` model is reused.** `VersionBumpPR` links to a `TestRun`
via foreign key rather than duplicating YARF tracking logic. The existing
`sync_test_runs` and `poll_for_gh_run_id` orchestration continues to work
unchanged.

---

## Implementation Order

Phases should be implemented in order (7 → 12). Each phase is independently
shippable and testable. Phases 7–9 do not require lemonade-server to be running.
