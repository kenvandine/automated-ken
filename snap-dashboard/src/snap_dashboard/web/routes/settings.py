"""Settings page routes."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from snap_dashboard.auth import get_current_user, get_user_config
from snap_dashboard.db.models import CollectionRun, Snap, UserConfig
from snap_dashboard.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _get_last_run(user_id: int):
    with get_session() as session:
        run = (
            session.query(CollectionRun)
            .filter_by(user_id=user_id, status="success")
            .order_by(CollectionRun.finished_at.desc())
            .first()
        )
        return run.finished_at if run and run.finished_at else None


@router.get("/settings", response_class=HTMLResponse)
async def settings_get(request: Request) -> HTMLResponse:
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]
    uc = get_user_config(user_id)

    with get_session() as session:
        snaps = (
            session.query(Snap)
            .filter_by(user_id=user_id)
            .order_by(Snap.name)
            .all()
        )
        snap_list = [
            {
                "name": s.name,
                "publisher": s.publisher,
                "manually_added": s.manually_added,
                "packaging_repo": s.packaging_repo,
                "upstream_repo": s.upstream_repo,
            }
            for s in snaps
        ]

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "config": uc,
            "snaps": snap_list,
            "last_run": _get_last_run(user_id),
            "intervals": [1, 6, 12, 24],
            "current_user": user,
        },
    )


@router.post("/settings")
async def settings_post(
    request: Request,
    publisher: str = Form(default=""),
    github_token: str = Form(default=""),
    interval: int = Form(default=6),
    testing_repo: str = Form(default=""),
    auto_test: str = Form(default=""),
) -> RedirectResponse:
    """Save per-user settings to UserConfig in the database."""
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]
    _auto_test = auto_test in ("1", "true", "on", "yes")

    with get_session() as session:
        uc = session.query(UserConfig).filter_by(user_id=user_id).first()
        if uc is None:
            uc = UserConfig(user_id=user_id)
            session.add(uc)
        if publisher.strip():
            uc.publisher = publisher.strip()
        if github_token.strip():
            uc.github_token = github_token.strip()
        uc.collect_interval_hours = interval
        uc.testing_repo = testing_repo.strip()
        uc.auto_test = _auto_test

    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/remove/{snap_name}")
async def settings_remove_snap(snap_name: str, request: Request) -> RedirectResponse:
    """Remove a snap from tracking."""
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]

    with get_session() as session:
        snap = session.query(Snap).filter_by(name=snap_name, user_id=user_id).first()
        if snap:
            session.delete(snap)

    return RedirectResponse(url="/settings", status_code=303)
