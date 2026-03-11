"""FastAPI application factory for snap-dashboard."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from snap_dashboard.db.session import init_db

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"
_TEMPLATES_DIR = Path(__file__).parent / "templates"

app = FastAPI(title="snap-dashboard", docs_url=None, redoc_url=None)

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
from snap_dashboard.web.routes import dashboard, onboarding, settings, snaps, testing  # noqa: E402

app.include_router(dashboard.router)
app.include_router(onboarding.router)
app.include_router(snaps.router)
app.include_router(settings.router)
app.include_router(testing.router)
