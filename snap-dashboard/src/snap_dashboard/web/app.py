"""FastAPI application factory for snap-dashboard."""

from __future__ import annotations

import logging
import secrets
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from snap_dashboard.config import get_config, save_config
from snap_dashboard.db.session import init_db

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"
_TEMPLATES_DIR = Path(__file__).parent / "templates"


class _RevalidatingStaticFiles(StaticFiles):
    """StaticFiles that always makes browsers revalidate before using a cached copy.

    Starlette's default StaticFiles response has no ``Cache-Control`` header
    at all, which leaves browsers free to apply heuristic caching (RFC 7234)
    and serve a stale ``style.css``/``app.js`` straight from disk cache after
    a snap rebuild+reinstall — the server-side fix is correct and already
    being served, but the browser never asks for it again. ``no-cache``
    still allows cheap conditional GETs (304 Not Modified via the existing
    ETag/Last-Modified handling) — it just forces a real round-trip to check
    for changes instead of trusting a heuristic freshness guess.
    """

    async def get_response(self, path: str, scope) -> object:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

app = FastAPI(title="Automated Ken", docs_url=None, redoc_url=None)

# Session middleware — secret comes from config.env / SESSION_SECRET env var
# (for a snap install, snap/hooks/configure generates and persists this via
# `snapctl set session-secret=...` the first time it runs, so it survives
# daemon restarts). If neither is set (e.g. a brand-new dev-mode checkout),
# generate one now and persist it to config.env so it's stable across
# restarts here too, instead of silently minting a new throwaway secret
# every time the process starts and logging everyone out.
_config = get_config()
_session_secret = _config.session_secret
if not _session_secret:
    _session_secret = secrets.token_hex(32)
    save_config({"SESSION_SECRET": _session_secret})
    logger.info(
        "No SESSION_SECRET configured — generated and persisted a new one "
        "to config.env so sessions survive restarts."
    )
app.add_middleware(SessionMiddleware, secret_key=_session_secret)

# Mount static files
app.mount("/static", _RevalidatingStaticFiles(directory=str(_STATIC_DIR)), name="static")

# Jinja2 templates (shared across routes)
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


@app.on_event("startup")
async def on_startup() -> None:
    """Initialise the database and start background agents on startup."""
    init_db()
    logger.info("Database initialised.")

    from snap_dashboard.agents.pr_monitor import PRMonitorAgent
    from snap_dashboard.agents.runner import get_runner
    from snap_dashboard.agents.scheduling import schedule_user_agents
    from snap_dashboard.auth import get_user_config
    from snap_dashboard.db.models import UserConfig
    from snap_dashboard.db.session import get_session as _gs

    runner = get_runner()

    # Schedule the per-user agents (release scanner, collector, stale build
    # scanner) for every user that already has a UserConfig row.
    # fire_immediately=True kicks off the first run within seconds so the
    # dashboard populates without waiting for the full interval to elapse.
    with _gs() as session:
        user_ids = [uc.user_id for uc in session.query(UserConfig).all()]
    for uid in user_ids:
        schedule_user_agents(runner, uid, get_user_config(uid), fire_immediately=True)

    # PR monitor runs every 5 minutes regardless of user count.
    runner.schedule_periodic(PRMonitorAgent, interval_hours=5 / 60)

    # Runner watchdog — clears stalled remote-runner jobs every 2 minutes.
    from snap_dashboard.agents.runner_watchdog import RunnerWatchdogAgent
    runner.schedule_periodic(RunnerWatchdogAgent, interval_hours=2 / 60)

    logger.info("Agent runner started.")

    # Start the bundled/embedded Lemonade server in the background so it
    # doesn't delay web server readiness (first-run installs a ~10MB binary
    # and warms up multi-GB opinionated per-task models — vision/text/coding,
    # see lemonade/models.py). Agents will simply see it as "not yet
    # available" and fall back to heuristics until it's ready.
    import threading

    from snap_dashboard.lemonade.embedded import get_embedded_manager

    threading.Thread(
        target=get_embedded_manager().ensure_started, daemon=True
    ).start()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    """Stop the embedded Lemonade subprocess cleanly."""
    from snap_dashboard.lemonade.embedded import get_embedded_manager

    get_embedded_manager().stop()


# Import and include routers after app is created to avoid circular imports
from snap_dashboard.web.routes import (  # noqa: E402
    admin,
    agents,
    auth,
    copilot_tasks,
    dashboard,
    docs,
    onboarding,
    runner_api,
    runners,
    settings,
    snaps,
    stats,
    testing,
    version_bumps,
)

app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(agents.router)
app.include_router(copilot_tasks.router)
app.include_router(dashboard.router)
app.include_router(docs.router)
app.include_router(onboarding.router)
app.include_router(runner_api.router)
app.include_router(runners.router)
app.include_router(snaps.router)
app.include_router(settings.router)
app.include_router(stats.router)
app.include_router(testing.router)
app.include_router(version_bumps.router)
