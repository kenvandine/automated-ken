# Demo Recording Setup

Automated browser demo for the Lemonade talk. Playwright drives the dashboard while
the in-process simulator advances the `lemonade-desktop` PR through the full pipeline
live on camera.

## Prerequisites

Install demo dependencies inside the snap-dashboard venv:

```sh
cd snap-dashboard
source .venv/bin/activate
pip install -r demo/requirements-demo.txt
playwright install chromium
```

`dbus-python` (included in `requirements-demo.txt`) is used to talk to the GNOME
Shell Screencast D-Bus API for Wayland-compatible screen recording. If the pip
build fails, install the system dev headers first:

```sh
sudo apt install libdbus-1-dev libglib2.0-dev
pip install dbus-python
```

## One-time database seed

Run this **once** before recording (safe to re-run — clears previous demo data first):

```sh
cd snap-dashboard
source .venv/bin/activate
python demo/seed_demo.py
```

This populates the database with:
- Version bump PRs for ghostty, thrive, warble, gemini-desktop, and lemonade-desktop
- Synthetic before/after screenshots (generated with Pillow)
- Agent run history showing ~20 recent operations
- Stale build triggers for terminal-fun, terminal-2048, terminal-tetris, terminal-solitaire

## Recording a video

**Step 1 — Start the server in demo mode (separate terminal):**

```sh
cd snap-dashboard
source .venv/bin/activate
SNAP_DASHBOARD_DEMO=1 snap-dashboard serve
```

The `SNAP_DASHBOARD_DEMO=1` flag:
- Starts the background pipeline simulator that advances `lemonade-desktop` through
  every stage live during the recording.
- Adds a `/demo-login` bypass so Playwright can authenticate without GitHub OAuth.
- Skips the real GitHub API call when merging a PR (just updates the DB).

**Step 2 — Run the Playwright driver (separate terminal):**

```sh
cd snap-dashboard
source .venv/bin/activate
python demo/run_demo.py

# If the server is on a non-default port:
SNAP_DASHBOARD_URL=http://127.0.0.1:19080 python demo/run_demo.py
```

The script opens a maximised Chromium window, navigates through the full demo, and
saves the recording to `demo/output/demo.webm`.

## Demo flow (~3.5 minutes)

| Time | What you see |
|------|-------------|
| 0:00 | Summary dashboard — channel comparison, attention-needed cards |
| 0:14 | Agent activity feed — agents running, pipeline stats |
| 0:24 | Version bumps list — PRs grouped by status |
| 0:32 | ghostty PR detail — before/after screenshots, AI reasoning (94% confidence) |
| 0:48 | Click Merge — ghostty moves to Merged |
| 0:56 | thrive PR detail — crash dialog in screenshot, AI rejection (91% confidence) |
| 1:20 | Agent feed — lemonade-desktop advancing (ci_pending → ci_passed → yarf_running) |
| 1:40 | Settings page — Lemonade server URL + model config |
| 1:54 | Agent feed — ⚡ Lemonade AI screenshot comparison firing live |
| 2:16 | lemonade-desktop PR detail — screenshots appear, 93% approve |
| 2:45 | Version bumps — lemonade-desktop → Stable Promoted |
| 3:15 | Dashboard — lemonade-desktop v10.4.0 now showing in stable |

## Post-processing the video

The output is `demo/output/demo.mp4` encoded by GNOME Shell's GStreamer pipeline.
To re-encode at higher quality or a different bitrate:

```sh
ffmpeg -i demo/output/demo.mp4 -c:v libx264 -preset slow -crf 18 demo/output/demo_final.mp4
```

## Troubleshooting

**Playwright can't click a link by text** — the selectors in `run_demo.py` fall back to
direct URL navigation (`/version-bumps/1`, `/version-bumps/5`, etc.) if the text match
fails. The IDs depend on seed order; check the IDs with:

```sh
python -c "
import sqlite3, os
db = os.path.expanduser('~/.local/share/snap-dashboard/snap-dashboard.db')
c = sqlite3.connect(db)
for r in c.execute('SELECT id, (SELECT name FROM snaps WHERE id=snap_id), status FROM version_bump_prs ORDER BY id'):
    print(r)
"
```

Update the fallback URLs in `run_demo.py` if the IDs differ.

**lemonade-desktop PR doesn't appear in agent_approved** — the simulator timing is
calibrated to match the Playwright navigation. If you pause or navigate faster, the
transition may not have fired yet. Wait a few seconds and reload the page.

**Session expired mid-demo** — increase the SessionMiddleware `max_age` or just ensure
the server was freshly started before the recording.
