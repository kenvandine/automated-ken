"""Dashboard routes — main landing page and refresh."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from snap_dashboard.auth import get_current_user, get_user_config
from snap_dashboard.db.models import CollectionRun, Snap
from snap_dashboard.db.session import get_session
from snap_dashboard.web.fleet import build_overview
from snap_dashboard.web.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()

_executor = ThreadPoolExecutor(max_workers=2)


def _get_last_run(user_id: int):
    """Return the most recent successful CollectionRun finished_at for this user."""
    with get_session() as session:
        run = (
            session.query(CollectionRun)
            .filter_by(user_id=user_id, status="success")
            .order_by(CollectionRun.finished_at.desc())
            .first()
        )
        if run and run.finished_at:
            return run.finished_at
    return None


@router.get("/", response_class=HTMLResponse)
async def dashboard_index(request: Request) -> HTMLResponse:
    """Overview: what needs you, what's in flight, what recently shipped.

    The full per-snap table lives on /snaps now; this page is the inbox.
    """
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]
    uc = get_user_config(user_id)

    if not uc.publisher:
        return RedirectResponse(url="/onboarding", status_code=302)

    with get_session() as session:
        snap_count = session.query(Snap).filter_by(user_id=user_id).count()
        if snap_count == 0:
            return RedirectResponse(url="/onboarding", status_code=302)
        overview = build_overview(session, user_id)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            **overview,
            "last_run": _get_last_run(user_id),
            "publisher": uc.publisher,
            "config": uc,
            "current_user": user,
        },
    )


def _run_collection_sync(user_id: int):
    from snap_dashboard.collector import run_collection
    uc = get_user_config(user_id)
    config = uc.to_config()
    return run_collection(config, user_id=user_id)


@router.post("/refresh")
async def refresh(background_tasks: BackgroundTasks, request: Request):
    """Trigger a background collection then redirect to dashboard."""
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]

    def _bg():
        try:
            _run_collection_sync(user_id)
        except Exception as exc:
            logger.error("Background collection failed: %s", exc)

    background_tasks.add_task(_bg)
    if request.headers.get("X-Requested-With"):
        return JSONResponse({"ok": True, "message": "Collection queued — new data appears in a minute or two."})
    return RedirectResponse(url="/", status_code=303)
