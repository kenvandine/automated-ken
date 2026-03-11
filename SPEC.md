# snap-dashboard — Project Specification

A personal snap maintenance dashboard for ken-vandine. Tracks channel versions,
open PRs, issues, and release status across all snaps published by (or maintained
by) a given publisher account.

---

## Goals

1. **Channel comparison** — for every snap, show the current revision/version in
   `stable`, `candidate`, `beta`, and `edge` so it is immediately obvious which
   channels need testing or promotion.
2. **Source / issue tracker overview** — surface open PRs and issues from the
   upstream and packaging repositories for each snap.
3. **Incremental discovery** — on first run the tool auto-discovers all snaps
   published by `ken-vandine` via the Snap Store API; subsequent runs reuse the
   persisted snap list and only fetch fresh data.
4. **Manual additions** — snaps the user maintains but does not publish under his
   own account can be added manually (via CLI or the web UI).
5. **Web dashboard** — a self-hosted web application presenting the data in a
   clear, scannable format.
6. **Snap-packaged** — the entire application ships as a snap.
7. **Scheduled updates** — a `systemd` timer (provided by the snap) refreshes
   data on a configurable interval (default: every 6 hours).

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│  snap-dashboard snap                                        │
│                                                             │
│  ┌──────────────┐   ┌──────────────┐   ┌────────────────┐  │
│  │  collector   │   │   store DB   │   │   web server   │  │
│  │  (Python)    │──▶│  (SQLite)    │◀──│  (FastAPI)     │  │
│  └──────────────┘   └──────────────┘   └────────────────┘  │
│         ▲                                       ▲           │
│         │ systemd timer                         │ browser   │
│  ┌──────┴──────┐                                │           │
│  │  snap-dashboard-collect.service              │           │
│  │  snap-dashboard-collect.timer                │           │
│  └─────────────┘                                │           │
└─────────────────────────────────────────────────┼───────────┘
                                                  │
                                              localhost:8080
```

### Components

| Component | Language / Framework | Purpose |
|-----------|---------------------|---------|
| `collector` | Python 3 | Queries Snap Store & GitHub/GitLab APIs, writes to SQLite |
| `web server` | Python 3 + FastAPI + Jinja2 | Serves the dashboard HTML + REST API |
| `CLI` | Python 3 (Click) | `snap-dashboard add`, `snap-dashboard collect`, `snap-dashboard serve` |
| `database` | SQLite (via SQLAlchemy) | Persists snap list, channel data, issues/PRs |
| `scheduler` | systemd timer unit | Triggers `snap-dashboard collect` on interval |

---

## Data Model

### `snaps` table

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | |
| `name` | TEXT UNIQUE | snap name, e.g. `gimp` |
| `publisher` | TEXT | Snap Store publisher account |
| `manually_added` | BOOLEAN | true if added via `snap-dashboard add` |
| `packaging_repo` | TEXT | URL of the snap packaging repo (GitHub/GitLab) |
| `upstream_repo` | TEXT | URL of the upstream project repo (optional) |
| `notes` | TEXT | free-form user notes |
| `created_at` | DATETIME | |
| `updated_at` | DATETIME | |

### `channel_map` table

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | |
| `snap_id` | INTEGER FK → snaps | |
| `channel` | TEXT | `stable`, `candidate`, `beta`, `edge` |
| `architecture` | TEXT | e.g. `amd64`, `arm64` |
| `revision` | INTEGER | |
| `version` | TEXT | |
| `released_at` | DATETIME | |
| `fetched_at` | DATETIME | |

### `issues` table

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | |
| `snap_id` | INTEGER FK → snaps | |
| `repo_url` | TEXT | which repo this came from |
| `issue_number` | INTEGER | |
| `title` | TEXT | |
| `state` | TEXT | `open` / `closed` |
| `type` | TEXT | `issue` or `pr` |
| `url` | TEXT | HTML URL |
| `author` | TEXT | |
| `created_at` | DATETIME | |
| `updated_at` | DATETIME | |
| `fetched_at` | DATETIME | |

### `collection_runs` table

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | |
| `started_at` | DATETIME | |
| `finished_at` | DATETIME | |
| `status` | TEXT | `success` / `partial` / `error` |
| `error_msg` | TEXT | |

---

## External API Integration

### Snap Store (snapcraft.io)

- **Publisher snap list**: `GET https://api.snapcraft.io/v2/snaps/find?publisher=ken-vandine`
  — returns all snaps published by the account (no auth required for public data).
- **Channel map**: `GET https://api.snapcraft.io/v2/snaps/info/{snap_name}`
  — returns full channel map with revision, version, and release dates.
- Rate-limit: polite 1 req/s default, configurable.

### GitHub

- **Issues + PRs**: GitHub REST API `/repos/{owner}/{repo}/issues?state=open`
  (issues endpoint returns both issues and PRs; filter by `pull_request` key).
- Auth: personal access token stored in `$SNAP_DATA/config.env` (or
  `~/.config/snap-dashboard/config.env` in dev mode). Token is optional; unauthenticated
  requests are limited to 60 req/hr.
- Supports GitLab MRs via GitLab REST API as a secondary source.

### Launchpad (optional / future)

- Some packaging repos live on Launchpad; stub out the interface now, implement later.

---

## Collector Logic

```
collect():
  1. If snaps table is empty (first run):
       → Query Snap Store for all snaps by publisher ken-vandine
       → Insert discovered snaps into DB with manually_added=False
  2. For each snap in DB:
       a. Fetch channel map from Snap Store → upsert channel_map rows
       b. If packaging_repo is set:
            → Fetch open issues & PRs → upsert issues rows
       c. If upstream_repo is set and differs from packaging_repo:
            → Fetch open issues & PRs → upsert issues rows
  3. Insert a collection_runs record with status and timestamp
```

The collector is intentionally idempotent — re-running always produces a
consistent, up-to-date DB state.

---

## CLI Interface

```
snap-dashboard collect          # run the collector once
snap-dashboard serve [--port N] # start the web server (default: 8080)
snap-dashboard add <snap-name>  # add a snap to the tracked list
    --packaging-repo <url>
    --upstream-repo  <url>
    --notes          <text>
snap-dashboard list             # print all tracked snaps
snap-dashboard remove <name>    # stop tracking a snap
snap-dashboard config set <key> <value>   # set a config value
snap-dashboard config get <key>
```

---

## Web Dashboard

### First-Run Onboarding Flow

When the database contains no publisher configuration the web UI shows a
full-screen onboarding wizard instead of the dashboard:

1. **Welcome step** — brief description of the tool.
2. **Publisher step** — text input for Snap Store publisher account (default
   `ken-vandine`). A "Verify" button hits the Snap Store API and shows the snap
   count found.
3. **GitHub token step** — optional PAT entry with a link to GitHub token docs.
   Token is persisted via `snapctl set github-token=...` (or to local config in
   dev mode).
4. **Done** — triggers the first `collect` run in the background and redirects
   to the summary dashboard.

### Pages / Views

#### `/` — Summary dashboard

- Fixed top navbar with the snap-dashboard logo (gradient text, Ubuntu font).
- "Last updated" timestamp + **Refresh now** button (triggers a background
  collect via a FastAPI background task, updates the page via HTMX or a simple
  redirect-after-POST).
- **Attention needed** cards: snaps where `edge` or `beta` is ahead of `stable`
  and no blocking issues — promote candidate highlighted prominently.
- **Snap table** with columns:
  - Snap name (links to Store page)
  - Stable | Candidate | Beta | Edge (version + revision, colour-coded: green =
    in sync, amber = newer revision available, red = channel missing/broken)
  - Open issues count (packaging + upstream repos combined)
  - Open PRs count
  - Last collected

#### `/snap/<name>` — Snap detail page

- Channel map with all tracked architectures.
- Version comparison between channels (edge → beta → candidate → stable lineage).
- Full issue / PR list from both repos, sorted by `updated_at`, filterable by
  type (issue / PR) and repo.
- Inline edit form for packaging repo URL, upstream repo URL, and notes.

#### `/settings` — Settings page

- Publisher account (saved via snapctl / config file).
- GitHub token field (write-only display, saved via `snap set`).
- Collection interval selector (1 h / 6 h / 12 h / 24 h).
- Manual snap management table (add/remove/edit).

#### `/snaps/add` — Add snap page (also accessible from `/settings`)

- Text input for snap name + **Search** button.
  - On search: queries `api.snapcraft.io/v2/snaps/info/{name}` for metadata.
  - Auto-populates: publisher, Store URL, and any `source-code` / `website`
    link found in snap metadata (used to pre-fill packaging/upstream repo URLs).
- Editable fields (pre-populated from search or entered manually):
  - Packaging repo URL
  - Upstream repo URL
  - Notes
- **Save** button adds the snap to the DB with `manually_added=True`.

### Technology

- **Backend**: FastAPI + Jinja2 templates + SQLAlchemy (sync, SQLite).
- **Frontend**: HTML + CSS + minimal vanilla JS; HTMX for async interactions
  (Refresh now, snap search). No heavy JS framework.
- **Styling**: See design system below. All assets bundled in the snap (no CDN).

---

## Visual Design System

The dashboard adopts the aesthetic established by the
[LinuxGroove](https://github.com/kenvandine/linuxgroove) project.

### Color Palette (CSS custom properties)

```css
:root {
  /* Backgrounds */
  --color-dark:    #0F0F23;   /* primary page background */
  --color-darker:  #0A0A15;   /* deeper contrast areas, cards */

  /* Text */
  --color-light:   #F8FAFC;
  --color-gray:    #64748B;   /* secondary / muted text */

  /* Accent spectrum */
  --color-pink:    #FF1B8D;
  --color-purple:  #A855F7;
  --color-blue:    #3B82F6;
  --color-cyan:    #06B6D4;   /* primary interactive / hover */
  --color-teal:    #14B8A6;
  --color-green:   #10B981;
  --color-yellow:  #F59E0B;
  --color-orange:  #F97316;
  --color-red:     #EF4444;
}
```

**Semantic usage in the dashboard:**
| Purpose | Color |
|---------|-------|
| Channel in-sync / no action needed | `--color-green` |
| Newer revision available upstream | `--color-yellow` |
| Channel missing / broken | `--color-red` |
| Promote candidate hint | `--color-cyan` gradient border |
| Open PRs badge | `--color-purple` |
| Open issues badge | `--color-orange` |
| Navbar logo gradient | pink → purple → blue → cyan |

### Typography

- **Primary font**: `Ubuntu, sans-serif` (loaded from bundled font files or
  system font stack; Google Fonts URL used only in dev mode).
- **Display / headings**: `Poppins` weight 700–800; responsive sizing via
  `clamp()`.
- **Body**: `1rem / 1.6` line height; muted secondary text in `--color-gray`.
- **Badges / tags**: `0.85rem`, weight 600, rounded pill (`border-radius: 20px`).

### Layout

- Max content width: `1200px`, centred, `20px` horizontal padding.
- Snap cards: `repeat(auto-fit, minmax(300px, 1fr))` CSS grid, `2rem` gap.
- Section padding: `80px` top/bottom (reduced from LinuxGroove's 120px for a
  denser data dashboard).

### Component Styles

**Navbar**
- Fixed top, `rgba(15, 15, 35, 0.95)` background + `backdrop-filter: blur(10px)`.
- Border-bottom: `1px solid rgba(255,255,255,0.1)`.
- Logo: animated gradient text (`gradientShift` 8s infinite).
- Nav links: hover reveals animated underline (pink → cyan gradient).

**Cards** (snap table rows rendered as cards on smaller viewports)
- Background: `rgba(255,255,255,0.05)`.
- Border: `1px solid rgba(255,255,255,0.1)`.
- `border-radius: 20px`, `backdrop-filter: blur(10px)`.
- Hover: `translateY(-5px)`, border shifts to `--color-purple` / `--color-cyan`,
  shadow: `0 20px 60px rgba(168,85,247,0.2)`.

**Buttons**
- Primary (e.g. Refresh, Search, Save):
  `background: linear-gradient(135deg, var(--color-pink), var(--color-purple))`,
  white text, `border-radius: 50px`, `padding: 0.75rem 2rem`.
- Secondary (e.g. Cancel, Edit):
  transparent + `1px solid var(--color-cyan)`, cyan text; fills on hover.

**Channel version badges**
- Pill-shaped (`border-radius: 20px`), `0.85rem` text.
- In-sync: green background tint + green border.
- Behind: amber background tint + amber border.
- Missing: red background tint + red border.

**Attention-needed section**
- Top of the dashboard as horizontal scrollable row of highlighted cards.
- Each card has a coloured left border (cyan for "ready to promote",
  orange for "has open blockers").
- Animated gradient overlay on hover (same sweep as LinuxGroove app cards).

### Animations

| Animation | Duration | Usage |
|-----------|----------|-------|
| `gradientShift` | 8s infinite | Logo gradient, hero text |
| `fadeInUp` | 0.6s ease-out | Cards appear on load, staggered 0.1s |
| `float` | 20s infinite | Background paint-splatter blobs (hero / onboarding) |

Background decorative blobs (3 absolutely-positioned radial gradients, blurred
`100px`, opacity `0.15`) are present on the onboarding/welcome screen for
ambiance; the main dashboard keeps them very subtle to avoid distracting from data.

### Responsive Breakpoints

| Breakpoint | Change |
|------------|--------|
| ≤ 768px | Hamburger nav, single-column card grid, full-width buttons |
| ≤ 480px | Reduced card padding (`1.5rem`), hide decorative blobs |

---

## Snap Packaging

### `snap/snapcraft.yaml` (outline)

```yaml
name: snap-dashboard
base: core24
version: git
summary: Personal snap maintenance dashboard
description: |
  Tracks channel versions, open issues, and PRs for snaps you maintain.
  Provides a web dashboard and scheduled background collection.
grade: stable
confinement: strict

# User-configurable snap options (snap set snap-dashboard <key>=<value>)
# bind:         bind address (default: 127.0.0.1)
# port:         listen port   (default: 8080)
# github-token: GitHub PAT    (default: "")
# publisher:    Store account  (default: "")
# interval:     collect interval in hours (default: 6)

apps:
  snap-dashboard:
    command: bin/snap-dashboard
    plugs: [network, home]

  collect:
    command: bin/snap-dashboard collect
    daemon: oneshot
    timer: 00:00~24:00/4      # 4 times per day, random spread
    plugs: [network]

  serve:
    command: bin/snap-dashboard serve
    daemon: simple
    plugs: [network-bind]

parts:
  snap-dashboard:
    plugin: python
    source: .
    python-requirements: [requirements.txt]
```

### Snap configuration hooks

`snap/hooks/configure` — shell hook called whenever `snap set` is used.
Reads the new values via `snapctl get` and writes a runtime config file to
`$SNAP_DATA/config.env` consumed by both the collector and the web server.
Restarts the `serve` daemon if bind address or port changes.

```sh
#!/bin/sh
set -e
bind=$(snapctl get bind)
port=$(snapctl get port)
token=$(snapctl get github-token)
publisher=$(snapctl get publisher)
interval=$(snapctl get interval)

# Write runtime config
cat > "$SNAP_DATA/config.env" <<EOF
BIND=${bind:-127.0.0.1}
PORT=${port:-8080}
GITHUB_TOKEN=${token:-}
PUBLISHER=${publisher:-}
COLLECT_INTERVAL_HOURS=${interval:-6}
EOF

# Restart web server to pick up new bind/port
snapctl restart snap-dashboard.serve || true
```

### Data locations

| Environment | Path |
|-------------|------|
| Snap (runtime) | `$SNAP_DATA/snap-dashboard.db` |
| Snap (config) | `$SNAP_DATA/config.env` |
| Dev / non-snap | `~/.local/share/snap-dashboard/` |

---

## Configuration

Stored as a simple `KEY=VALUE` file (or environment variables):

```
PUBLISHER=ken-vandine
GITHUB_TOKEN=ghp_...
COLLECT_INTERVAL_HOURS=6
SERVE_PORT=8080
LOG_LEVEL=INFO
```

---

## Implementation Phases

### Phase 1 — Core data pipeline
- [ ] Project scaffold: `pyproject.toml`, `src/snap_dashboard/` layout
- [ ] SQLAlchemy models (`snaps`, `channel_map`, `collection_runs`)
- [ ] Snap Store API client (publisher discovery + channel map)
- [ ] Collector CLI command (`snap-dashboard collect`)
- [ ] Basic `snap-dashboard list` output

### Phase 2 — Web dashboard (read-only)
- [ ] FastAPI app skeleton + Jinja2 base template (LinuxGroove design system)
- [ ] Shared CSS (`static/style.css`) with CSS variables, fonts, card/button/badge components
- [ ] First-run onboarding wizard (`/onboarding`)
- [ ] Summary page (`/`) with snap table and channel comparison
- [ ] Snap detail page (`/snap/<name>`)
- [ ] `snap-dashboard serve` command, respects `BIND` / `PORT` env vars

### Phase 3 — Issue / PR integration
- [ ] SQLAlchemy model (`issues`)
- [ ] GitHub API client (issues + PRs)
- [ ] GitLab API client stub
- [ ] Surface issue/PR counts on dashboard pages

### Phase 4 — Management UI
- [ ] Settings page (publisher, GitHub token read from snapctl/config, interval)
- [ ] `/snaps/add` page with snap name search (auto-populates repo URLs from
      Store metadata `source-code` / `website` fields)
- [ ] Add/edit snap form (packaging repo, upstream repo, notes, manual override)
- [ ] "Refresh now" button (async background collect via FastAPI background task)
- [ ] Manual snap management CLI (`add`, `remove`, `list`)

### Phase 5 — Snap packaging
- [ ] `snap/snapcraft.yaml` with `apps.serve`, `apps.collect` (timer daemon)
- [ ] `snap/hooks/configure` — reads `snap set` values, writes `$SNAP_DATA/config.env`
- [ ] Default snap config: `bind=127.0.0.1`, `port=8080`, `interval=6`
- [ ] Snap interface plugs (`network`, `network-bind`, `home`)
- [ ] Test install from local build; verify `snap set` round-trips correctly

### Phase 6 — Polish
- [ ] Dark-mode CSS, responsive layout
- [ ] "Attention needed" promotion hints
- [ ] Architecture-aware channel comparison
- [ ] Launchpad stub
- [ ] README + contribution guide

---

## Out of Scope (for now)

- Automated snap promotion (write access to the Store).
- Email / Slack notifications.
- Multi-user accounts or authentication on the web UI (localhost-only by default).
- Full changelog parsing (versions only, not commit diffs).

---

## Resolved Design Decisions

1. **Web UI binding** — defaults to `127.0.0.1` (localhost only).
   Overridable via `snap set snap-dashboard bind=0.0.0.0` to expose on all
   interfaces. Port configurable via `snap set snap-dashboard port=8080`.

2. **GitHub token** — `snap set snap-dashboard github-token=ghp_...` stored in
   snapd config (integrated with snapd's credential store, not a world-readable
   file). Read at runtime via `snapctl get github-token`.

3. **Launchpad packaging repos** — deferred to Phase 6; interface stubbed in Phase 3.

---

## Open Questions

1. Launchpad packaging repos: full implementation in Phase 6 only?
   → Yes, stub the interface during Phase 3.
