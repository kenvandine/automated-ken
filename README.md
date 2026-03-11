# snap-dashboard

A personal maintenance dashboard for snap publishers. Tracks channel versions,
open issues, and pull requests across all snaps you publish or maintain — served
as a self-hosted web application and packaged as a snap with a built-in scheduler.

## Features

- **Channel comparison** — see `stable`, `candidate`, `beta`, and `edge` versions
  side-by-side for every snap, colour-coded so promotable updates stand out
- **Attention-needed highlights** — snaps where edge/beta is ahead of stable are
  surfaced at the top of the dashboard so nothing falls through the cracks
- **Issue & PR tracking** — open issues and pull requests are fetched from GitHub
  and GitLab packaging repos and shown per snap
- **Auto-discovery** — on first run, all snaps published by the configured
  publisher account are discovered automatically via the Snap Store API
- **Manual additions** — add snaps you maintain but don't publish yourself via
  the web UI or CLI; the web UI can search the Store to auto-populate repo URLs
- **Scheduled collection** — a `systemd` timer (provided by the snap) refreshes
  data every 6 hours by default, configurable via `snap set`
- **LinuxGroove-inspired UI** — dark glassmorphic design with animated gradients,
  responsive layout, and mobile support

## Quick start (development)

```sh
git clone https://github.com/kenvandine/automated-ken
cd automated-ken/snap-dashboard

python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Run a first collection (discovers all snaps by the given publisher)
PUBLISHER=ken-vandine snap-dashboard collect

# Start the web server
snap-dashboard serve          # http://127.0.0.1:8080
```

Open `http://127.0.0.1:8080` in a browser. If no publisher is configured you
will be taken through a short onboarding wizard.

## Snap installation

```sh
sudo snap install snap-dashboard
```

After install, configure your publisher account and optionally a GitHub token:

```sh
snap set snap-dashboard publisher=ken-vandine
snap set snap-dashboard github-token=ghp_YOUR_TOKEN
```

The `serve` daemon starts automatically. Open `http://127.0.0.1:8080`.

### Configuration via `snap set`

| Key | Default | Description |
|-----|---------|-------------|
| `publisher` | _(empty)_ | Snap Store publisher account to auto-discover |
| `github-token` | _(empty)_ | GitHub personal access token (increases rate limits) |
| `bind` | `127.0.0.1` | Bind address; set to `0.0.0.0` to expose on all interfaces |
| `port` | `8080` | HTTP port for the web dashboard |
| `interval` | `6` | Collection interval in hours |

```sh
# Expose on all interfaces (e.g. behind a reverse proxy)
snap set snap-dashboard bind=0.0.0.0 port=8080
```

Any `snap set` change triggers the `configure` hook which rewrites the runtime
config and restarts the `serve` daemon automatically.

## CLI reference

```
snap-dashboard collect          Run the data collector once
snap-dashboard serve            Start the web server (honours BIND / PORT env)
  --port  N                     Override listen port
  --bind  HOST                  Override bind address

snap-dashboard add <name>       Track a snap not published under your account
  --packaging-repo URL          URL of the snap packaging repository
  --upstream-repo  URL          URL of the upstream project repository
  --notes          TEXT         Free-form notes

snap-dashboard list             Print all tracked snaps with current channel versions
snap-dashboard remove <name>    Stop tracking a snap
```

## Web dashboard

### Pages

| Path | Description |
|------|-------------|
| `/` | Summary dashboard — channel comparison table + attention-needed cards |
| `/snap/<name>` | Snap detail — full channel map, issue/PR list, edit repo URLs |
| `/snaps/add` | Add a snap — search the Store, auto-populate repo URLs, save |
| `/settings` | Publisher, GitHub token, collection interval, manual snap list |
| `/onboarding` | First-run wizard (shown automatically when no publisher is set) |

### Adding a snap manually

1. Go to **Settings → Add snap** (or navigate to `/snaps/add`)
2. Enter the snap name and click **Search** — the Store is queried and
   `packaging_repo` / `upstream_repo` are auto-populated from the snap's
   `source` and `website` links
3. Review or override the URLs, add optional notes, click **Save**
4. Click **Refresh now** on the dashboard to collect issue/PR data immediately

## Project structure

```
snap-dashboard/
├── src/snap_dashboard/
│   ├── cli.py              Click CLI entry point
│   ├── collector.py        Data collection pipeline
│   ├── config.py           Config loader (env → config.env → defaults)
│   ├── db/
│   │   ├── models.py       SQLAlchemy ORM models
│   │   └── session.py      DB session factory + init_db()
│   ├── store/
│   │   └── client.py       Snap Store API v2 client
│   ├── github/
│   │   └── client.py       GitHub + GitLab issues/PR client
│   └── web/
│       ├── app.py          FastAPI application
│       ├── routes/         Route handlers (dashboard, snaps, settings, onboarding)
│       ├── templates/      Jinja2 HTML templates
│       └── static/         CSS + JS assets
├── snap/
│   ├── snapcraft.yaml      Snap package definition
│   └── hooks/configure     snap set handler
└── bin/snap-dashboard      Wrapper script for the snap
```

## Data collected

All data is stored locally in SQLite at:
- **Snap (runtime):** `$SNAP_DATA/snap-dashboard.db`
- **Dev mode:** `~/.local/share/snap-dashboard/snap-dashboard.db`

No data is ever sent to a third party. The tool only reads from:
- `https://api.snapcraft.io` — public snap metadata and channel maps
- `https://api.github.com` — public or token-authenticated repository data
- `https://gitlab.com/api/v4` — public GitLab repository data (optional)

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
| `httpx` | HTTP client for Store + GitHub APIs |
| `jinja2` | HTML templating |
| `python-multipart` | Form parsing |

## License

MIT
