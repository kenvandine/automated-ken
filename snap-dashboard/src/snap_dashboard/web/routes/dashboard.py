"""Dashboard routes — main landing page and refresh."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from snap_dashboard.auth import get_current_user, get_user_config
from snap_dashboard.db.models import ChannelMap, CollectionRun, Issue, Snap, TestRun
from snap_dashboard.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

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


def _build_snap_rows(session, user_id: int):
    """Build the snap table rows with channel data and issue counts."""
    snaps = session.query(Snap).filter_by(user_id=user_id).order_by(Snap.name).all()
    rows = []
    attention = []

    for snap in snaps:
        # Channel map keyed by (channel, arch) -> version
        cm_rows = session.query(ChannelMap).filter_by(snap_id=snap.id).all()
        channels: dict[str, dict[str, str | None]] = {}
        for cm in cm_rows:
            if cm.channel not in channels:
                channels[cm.channel] = {}
            channels[cm.channel][cm.architecture] = cm.version

        def ver(ch: str) -> str | None:
            return (channels.get(ch) or {}).get("amd64")

        stable_ver = ver("stable")
        candidate_ver = ver("candidate")
        beta_ver = ver("beta")
        edge_ver = ver("edge")

        # Issue/PR counts
        issue_count = (
            session.query(Issue)
            .filter_by(snap_id=snap.id, type="issue", state="open")
            .count()
        )
        pr_count = (
            session.query(Issue)
            .filter_by(snap_id=snap.id, type="pr", state="open")
            .count()
        )

        # Latest channel_map fetched_at
        latest_cm = (
            session.query(ChannelMap)
            .filter_by(snap_id=snap.id)
            .order_by(ChannelMap.fetched_at.desc())
            .first()
        )
        last_collected = latest_cm.fetched_at if latest_cm else None

        row = {
            "snap": snap,
            "stable": stable_ver,
            "candidate": candidate_ver,
            "beta": beta_ver,
            "edge": edge_ver,
            "issue_count": issue_count,
            "pr_count": pr_count,
            "last_collected": last_collected,
        }
        rows.append(row)

        # Attention needed: edge/beta ahead of stable
        if edge_ver and edge_ver != stable_ver:
            label = "ready_to_promote" if (issue_count + pr_count) == 0 else "has_open_items"
            attention.append(
                {
                    "snap": snap,
                    "edge_ver": edge_ver,
                    "stable_ver": stable_ver,
                    "issue_count": issue_count,
                    "pr_count": pr_count,
                    "label": label,
                }
            )

    return rows, attention


@router.get("/", response_class=HTMLResponse)
async def dashboard_index(request: Request) -> HTMLResponse:
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

        rows, attention = _build_snap_rows(session, user_id)
        last_run = _get_last_run(user_id)

        # Build a dict of snap_name → most recent non-promoted TestRun for badge display
        active_runs = (
            session.query(TestRun)
            .filter_by(user_id=user_id)
            .filter(TestRun.promoted.is_(False))
            .order_by(TestRun.started_at.desc())
            .all()
        )
        test_runs_by_snap: dict[str, dict] = {}
        for run in active_runs:
            if run.snap_name not in test_runs_by_snap:
                test_runs_by_snap[run.snap_name] = {
                    "status": run.status,
                    "pr_number": run.pr_number,
                    "version": run.version,
                }

        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "rows": rows,
                "attention": attention,
                "last_run": last_run,
                "publisher": uc.publisher,
                "config": uc,
                "test_runs_by_snap": test_runs_by_snap,
                "current_user": user,
            },
        )


def _run_collection_sync(user_id: int):
    from snap_dashboard.collector import run_collection
    uc = get_user_config(user_id)
    config = uc.to_config()
    with get_session() as session:
        return run_collection(session, config, user_id=user_id)


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
    return RedirectResponse(url="/", status_code=303)
