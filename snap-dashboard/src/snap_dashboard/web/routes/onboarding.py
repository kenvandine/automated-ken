"""Onboarding wizard routes."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from snap_dashboard.config import get_config, save_config
from snap_dashboard.store.client import find_snaps_by_publisher

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/onboarding")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

# Simple in-memory state for the onboarding wizard
_verify_result: dict = {}


@router.get("", response_class=HTMLResponse)
async def onboarding_get(request: Request, step: int = 1) -> HTMLResponse:
    config = get_config()
    return templates.TemplateResponse(
        "onboarding.html",
        {
            "request": request,
            "step": step,
            "publisher": config.publisher or "ken-vandine",
            "verify_result": _verify_result,
            "last_run": None,
        },
    )


@router.post("/publisher")
async def onboarding_publisher(
    request: Request,
    publisher: str = Form(...),
) -> RedirectResponse:
    """Validate publisher and save config."""
    global _verify_result
    snaps = find_snaps_by_publisher(publisher)
    _verify_result = {
        "publisher": publisher,
        "snap_count": len(snaps),
        "found": len(snaps) > 0,
    }
    if snaps:
        save_config({"PUBLISHER": publisher})
    return RedirectResponse(url="/onboarding?step=2", status_code=303)


@router.post("/token")
async def onboarding_token(
    request: Request,
    github_token: str = Form(default=""),
) -> RedirectResponse:
    """Save GitHub token."""
    if github_token.strip():
        save_config({"GITHUB_TOKEN": github_token.strip()})
    return RedirectResponse(url="/onboarding?step=3", status_code=303)


@router.post("/complete")
async def onboarding_complete(
    background_tasks: BackgroundTasks,
    request: Request,
) -> RedirectResponse:
    """Trigger first collection and redirect to dashboard."""

    def _first_collect():
        from snap_dashboard.collector import run_collection
        config = get_config()
        from snap_dashboard.db.session import get_session
        with get_session() as session:
            run_collection(session, config)

    background_tasks.add_task(_first_collect)
    return RedirectResponse(url="/", status_code=303)
