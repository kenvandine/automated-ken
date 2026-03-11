"""Settings page routes."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from snap_dashboard.config import get_config, save_config
from snap_dashboard.db.models import CollectionRun, Snap
from snap_dashboard.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _get_last_run():
    with get_session() as session:
        run = (
            session.query(CollectionRun)
            .filter(CollectionRun.status == "success")
            .order_by(CollectionRun.finished_at.desc())
            .first()
        )
        return run.finished_at if run and run.finished_at else None


@router.get("/settings", response_class=HTMLResponse)
async def settings_get(request: Request) -> HTMLResponse:
    config = get_config()
    with get_session() as session:
        snaps = session.query(Snap).order_by(Snap.name).all()
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
            "config": config,
            "snaps": snap_list,
            "last_run": _get_last_run(),
            "intervals": [1, 6, 12, 24],
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
    """Save settings and redirect."""
    updates: dict[str, str] = {}
    if publisher.strip():
        updates["PUBLISHER"] = publisher.strip()
    if github_token.strip():
        updates["GITHUB_TOKEN"] = github_token.strip()
    updates["COLLECT_INTERVAL_HOURS"] = str(interval)
    updates["TESTING_REPO"] = testing_repo.strip()
    updates["AUTO_TEST"] = "true" if auto_test in ("1", "true", "on", "yes") else "false"
    save_config(updates)
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/remove/{snap_name}")
async def settings_remove_snap(snap_name: str, request: Request) -> RedirectResponse:
    """Remove a snap from tracking."""
    with get_session() as session:
        snap = session.query(Snap).filter_by(name=snap_name).first()
        if snap:
            session.delete(snap)
    return RedirectResponse(url="/settings", status_code=303)
