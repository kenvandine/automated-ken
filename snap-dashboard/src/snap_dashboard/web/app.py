"""FastAPI application factory for snap-dashboard."""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from snap_dashboard.db.session import init_db

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"
_TEMPLATES_DIR = Path(__file__).parent / "templates"

app = FastAPI(title="snap-dashboard", docs_url=None, redoc_url=None)

# Session middleware — secret read from env or auto-generated.
# The full config.py (which reads config.env) is not used here to avoid
# circular import issues and to ensure middleware is registered before startup.
_session_secret = os.environ.get("SESSION_SECRET", "")
if not _session_secret:
    _session_secret = secrets.token_hex(32)
    logger.warning(
        "SESSION_SECRET not set — using a random key. "
        "Sessions will not persist across restarts. "
        "Set SESSION_SECRET in your config for production use."
    )
app.add_middleware(SessionMiddleware, secret_key=_session_secret)

# Mount static files
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# Jinja2 templates (shared across routes)
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


@app.on_event("startup")
async def on_startup() -> None:
    """Initialise the database on startup."""
    init_db()
    logger.info("Database initialised.")


# Import and include routers after app is created to avoid circular imports
from snap_dashboard.web.routes import (  # noqa: E402
    admin,
    auth,
    dashboard,
    docs,
    onboarding,
    settings,
    snaps,
    testing,
)

app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(dashboard.router)
app.include_router(docs.router)
app.include_router(onboarding.router)
app.include_router(snaps.router)
app.include_router(settings.router)
app.include_router(testing.router)
