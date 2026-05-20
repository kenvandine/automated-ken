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
    lemonade_server_url: str = Form(default=""),
    lemonade_model: str = Form(default=""),
    bot_github_token: str = Form(default=""),
    bot_github_login: str = Form(default=""),
    agent_interval_hours: int = Form(default=4),
    auto_merge: str = Form(default=""),
    auto_promote: str = Form(default=""),
    auto_promote_confidence: float = Form(default=0.85),
    auto_rebuild_stale: str = Form(default=""),
    stale_build_days: int = Form(default=30),
) -> RedirectResponse:
    """Save per-user settings to UserConfig in the database."""
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]
    _auto_test = auto_test in ("1", "true", "on", "yes")
    _auto_merge = auto_merge in ("1", "true", "on", "yes")
    _auto_promote = auto_promote in ("1", "true", "on", "yes")
    _auto_rebuild_stale = auto_rebuild_stale in ("1", "true", "on", "yes")

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
        # Agent / AI settings
        if lemonade_server_url.strip():
            uc.lemonade_server_url = lemonade_server_url.strip()
        if lemonade_model.strip():
            uc.lemonade_model = lemonade_model.strip()
        if bot_github_token.strip():
            uc.bot_github_token = bot_github_token.strip()
        if bot_github_login.strip():
            uc.bot_github_login = bot_github_login.strip()
        uc.agent_interval_hours = agent_interval_hours
        uc.auto_merge = _auto_merge
        uc.auto_promote = _auto_promote
        uc.auto_promote_confidence = max(0.0, min(1.0, auto_promote_confidence))
        uc.auto_rebuild_stale = _auto_rebuild_stale
        uc.stale_build_days = max(1, stale_build_days)

    # Reschedule agents with the new settings
    try:
        from snap_dashboard.agents.release_scanner import ReleaseScannerAgent
        from snap_dashboard.agents.stale_build_scanner import StaleSnapScannerAgent
        from snap_dashboard.agents.runner import get_runner
        runner = get_runner()
        runner.reschedule(ReleaseScannerAgent, interval_hours=agent_interval_hours, user_id=user_id)
        runner.reschedule(StaleSnapScannerAgent, interval_hours=24, user_id=user_id)
    except Exception as exc:
        logger.warning("failed to reschedule agents: %s", exc)

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
