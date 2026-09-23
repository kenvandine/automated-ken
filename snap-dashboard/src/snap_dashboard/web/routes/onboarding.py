"""Onboarding wizard routes."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from snap_dashboard.auth import get_current_user, get_user_config
from snap_dashboard.db.models import UserConfig
from snap_dashboard.db.session import get_session
from snap_dashboard.store.client import find_snaps_by_publisher

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/onboarding")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

# Simple in-memory state for the onboarding wizard (per-request is fine)
_verify_result: dict = {}


@router.get("", response_class=HTMLResponse)
async def onboarding_get(request: Request, step: int = 1) -> HTMLResponse:
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    uc = get_user_config(user["id"])
    return templates.TemplateResponse(
        "onboarding.html",
        {
            "request": request,
            "step": step,
            "publisher": uc.publisher or "",
            "verify_result": _verify_result,
            "last_run": None,
            "current_user": user,
        },
    )


@router.post("/publisher")
async def onboarding_publisher(
    request: Request,
    publisher: str = Form(...),
) -> RedirectResponse:
    """Validate publisher and save to UserConfig."""
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    global _verify_result
    snaps = find_snaps_by_publisher(publisher)
    _verify_result = {
        "publisher": publisher,
        "snap_count": len(snaps),
        "found": len(snaps) > 0,
    }
    if snaps:
        with get_session() as session:
            uc = session.query(UserConfig).filter_by(user_id=user["id"]).first()
            if uc is None:
                uc = UserConfig(user_id=user["id"])
                session.add(uc)
            uc.publisher = publisher
        return RedirectResponse(url="/onboarding?step=3", status_code=303)

    return RedirectResponse(url="/onboarding?step=2", status_code=303)


@router.post("/token")
async def onboarding_token(
    request: Request,
    github_token: str = Form(default=""),
) -> RedirectResponse:
    """Save GitHub token to UserConfig."""
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    if github_token.strip():
        with get_session() as session:
            uc = session.query(UserConfig).filter_by(user_id=user["id"]).first()
            if uc is None:
                uc = UserConfig(user_id=user["id"])
                session.add(uc)
            uc.github_token = github_token.strip()

    return RedirectResponse(url="/onboarding?step=3", status_code=303)


@router.post("/complete")
async def onboarding_complete(
    background_tasks: BackgroundTasks,
    request: Request,
) -> RedirectResponse:
    """Trigger first collection, start this user's background agents, and redirect."""
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]

    def _first_collect():
        from snap_dashboard.collector import run_collection
        uc = get_user_config(user_id)
        config = uc.to_config()
        with get_session() as session:
            run_collection(session, config, user_id=user_id)

    background_tasks.add_task(_first_collect)

    # Previously, a brand-new user's release scanner / collector / stale
    # build scanner were never scheduled until the whole server process was
    # restarted, because app startup only schedules agents for UserConfig
    # rows that already existed at boot time. Schedule them here too so the
    # agents advertised on first login actually start working immediately.
    try:
        from snap_dashboard.agents.runner import get_runner
        from snap_dashboard.agents.scheduling import schedule_user_agents
        schedule_user_agents(get_runner(), user_id, get_user_config(user_id), fire_immediately=True)
    except Exception as exc:
        logger.warning("failed to schedule agents for new user %s: %s", user_id, exc)

    return RedirectResponse(url="/", status_code=303)
