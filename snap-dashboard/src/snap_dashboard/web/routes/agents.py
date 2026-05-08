"""Agent activity feed routes."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from snap_dashboard.auth import get_current_user
from snap_dashboard.db.models import AgentRun, UpstreamRelease
from snap_dashboard.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request) -> HTMLResponse:
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]

    with get_session() as session:
        runs = (
            session.query(AgentRun)
            .filter_by(user_id=user_id)
            .order_by(AgentRun.started_at.desc())
            .limit(100)
            .all()
        )
        run_list = [
            {
                "id": r.id,
                "agent_type": r.agent_type,
                "snap_name": r.snap_name or "",
                "status": r.status,
                "result_summary": r.result_summary or "",
                "error_msg": r.error_msg or "",
                "started_at": r.started_at,
                "finished_at": r.finished_at,
            }
            for r in runs
        ]

        pending_releases = (
            session.query(UpstreamRelease)
            .join(UpstreamRelease.snap)
            .filter_by(user_id=user_id)
            .filter(UpstreamRelease.acted_on.is_(False))
            .order_by(UpstreamRelease.discovered_at.desc())
            .all()
        )
        release_list = [
            {
                "id": r.id,
                "snap_name": r.snap.name if r.snap else "",
                "part_name": r.part_name,
                "current_version": r.current_version or "unknown",
                "latest_version": r.latest_version,
                "release_url": r.release_url or "",
                "discovered_at": r.discovered_at,
            }
            for r in pending_releases
        ]

    return templates.TemplateResponse(
        "agents.html",
        {
            "request": request,
            "current_user": user,
            "agent_runs": run_list,
            "pending_releases": release_list,
            "last_run": None,
        },
    )


@router.post("/agents/scan-now")
async def scan_now(request: Request) -> RedirectResponse:
    """Trigger an immediate release scan for the current user."""
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    from snap_dashboard.agents.release_scanner import ReleaseScannerAgent
    from snap_dashboard.agents.runner import get_runner
    get_runner().submit(ReleaseScannerAgent(user_id=user["id"]))

    return RedirectResponse(url="/agents", status_code=303)


@router.get("/api/events")
async def sse_events(request: Request):
    """Server-Sent Events stream for live agent and version-bump updates."""
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]

    async def event_generator():
        import asyncio
        last_agent_id = 0
        last_bump_id = 0
        try:
            while True:
                if await request.is_disconnected():
                    break

                events: list[str] = []

                with get_session() as session:
                    # New agent runs
                    new_runs = (
                        session.query(AgentRun)
                        .filter(
                            AgentRun.user_id == user_id,
                            AgentRun.id > last_agent_id,
                        )
                        .order_by(AgentRun.id)
                        .limit(20)
                        .all()
                    )
                    for r in new_runs:
                        last_agent_id = r.id
                        payload = json.dumps({
                            "id": r.id,
                            "agent_type": r.agent_type,
                            "snap_name": r.snap_name or "",
                            "status": r.status,
                            "summary": r.result_summary or r.error_msg or "",
                        })
                        events.append(f"event: agent_status\ndata: {payload}\n\n")

                    # Version bump changes
                    from snap_dashboard.db.models import VersionBumpPR
                    new_bumps = (
                        session.query(VersionBumpPR)
                        .filter(
                            VersionBumpPR.user_id == user_id,
                            VersionBumpPR.id > last_bump_id,
                        )
                        .order_by(VersionBumpPR.id)
                        .limit(20)
                        .all()
                    )
                    for b in new_bumps:
                        last_bump_id = b.id
                        payload = json.dumps({
                            "id": b.id,
                            "snap_name": b.snap.name if b.snap else "",
                            "old_version": b.old_version or "",
                            "new_version": b.new_version or "",
                            "status": b.status,
                            "pr_url": b.bot_pr_url or "",
                        })
                        events.append(f"event: version_bump_update\ndata: {payload}\n\n")

                for event in events:
                    yield event

                if not events:
                    yield ": keepalive\n\n"

                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
