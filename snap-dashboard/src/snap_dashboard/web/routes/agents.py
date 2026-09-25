"""Agent activity feed routes — mission control dashboard."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from snap_dashboard.auth import get_current_user, get_user_config
from snap_dashboard.db.models import AgentRun, TestRun, UpstreamRelease, VersionBumpPR
from snap_dashboard.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@router.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request) -> HTMLResponse:
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)
    return templates.TemplateResponse(
        request,
        "agents.html",
        {"current_user": user, "last_run": None},
    )


# ---------------------------------------------------------------------------
# JSON status API — polled by the dashboard
# ---------------------------------------------------------------------------

@router.get("/api/agent-status")
def agent_status(request: Request) -> JSONResponse:
    """Return live agent state, pipeline counts, lemonade status, and schedules."""
    user = get_current_user(request)
    if user is None:
        return JSONResponse({"error": "not authenticated"}, status_code=401)

    user_id = user["id"]
    uc = get_user_config(user_id)

    from snap_dashboard.agents.runner import get_runner, get_tracker
    tracker = get_tracker()
    runner = get_runner()

    # Active agents from in-memory tracker (this user's, plus global ones)
    active = tracker.get_active(user_id)

    # Recent activity log (last 40 entries)
    log = tracker.get_log_since(max(0, tracker.latest_seq() - 40), user_id)

    # Pipeline stage counts from DB
    with get_session() as session:
        new_releases = (
            session.query(UpstreamRelease)
            .join(UpstreamRelease.snap)
            .filter_by(user_id=user_id)
            .filter(UpstreamRelease.acted_on.is_(False))
            .count()
        )
        def _bump_count(*statuses):
            return (
                session.query(VersionBumpPR)
                .filter(VersionBumpPR.user_id == user_id)
                .filter(VersionBumpPR.status.in_(statuses))
                .count()
            )

        # Not every TestRun is tied to a version-bump PR -- manually
        # triggered runs and runs against manually-added snaps have
        # ``pr_number is None`` (see testing/orchestrator.py) and their
        # progress lives entirely on TestRun.status/review_decision, which
        # the VersionBumpPR-based counts above never see. Without this,
        # the pipeline showed real "New Releases" activity but a
        # permanently-stuck 0 for every later stage whenever the live work
        # was happening via standalone test runs rather than bot PRs.
        standalone_runs = (
            session.query(TestRun)
            .filter(TestRun.user_id == user_id)
            .filter(TestRun.pr_number.is_(None))
            .filter(TestRun.status.in_(
                ("triggered", "running", "reviewing", "passed", "failed", "promoted")
            ))
            .all()
        )
        standalone_yarf_running = 0
        standalone_under_review = 0
        standalone_approved = 0
        standalone_merged = 0
        for r in standalone_runs:
            if r.status in ("triggered", "running"):
                standalone_yarf_running += 1
            elif r.status == "reviewing" or r.status == "failed":
                standalone_under_review += 1
            elif r.status == "passed":
                if r.review_decision == "approve":
                    standalone_approved += 1
                else:
                    standalone_under_review += 1
            elif r.status == "promoted":
                standalone_merged += 1

        pipeline = {
            "new_releases": new_releases,
            "prs_open": _bump_count("open", "ci_pending", "ci_passed", "ci_failed"),
            "yarf_running": _bump_count("yarf_running") + standalone_yarf_running,
            "under_review": _bump_count("yarf_passed", "yarf_failed", "needs_review") + standalone_under_review,
            "approved": _bump_count("agent_approved") + standalone_approved,
            "merged": _bump_count(
                "merged", "awaiting_release", "candidate_testing", "stable_promoted", "stable_promoted_partial"
            ) + standalone_merged,
        }

        # Recent agent run history (last 20)
        recent_runs = (
            session.query(AgentRun)
            .filter_by(user_id=user_id)
            .order_by(AgentRun.started_at.desc())
            .limit(20)
            .all()
        )
        history = [
            {
                "id": r.id,
                "agent_type": r.agent_type,
                "snap_name": r.snap_name or "",
                "status": r.status,
                "summary": r.result_summary or r.error_msg or "",
                "started_at": r.started_at.strftime("%H:%M:%S") if r.started_at else "",
                "duration_s": (
                    int((r.finished_at - r.started_at).total_seconds())
                    if r.finished_at and r.started_at else None
                ),
            }
            for r in recent_runs
        ]

    # Lemonade status
    lemonade_url = uc.lemonade_server_url or ""
    lemonade_model = uc.lemonade_model or ""
    lemonade_backend = getattr(uc, "lemonade_backend", "") or "embedded"
    lemonade_available = False
    try:
        from snap_dashboard.lemonade.client import get_lemonade_client
        client = get_lemonade_client(uc)  # never blocks — reflects current state only
        if client:
            lemonade_url = client.base_url
            lemonade_model = client.model
            lemonade_available = client.is_available()
    except Exception:
        pass

    # Schedule countdown
    schedules = [s for s in runner.get_schedules() if s["user_id"] in (None, user_id)]

    return JSONResponse({
        "active_agents": active,
        "activity_log": log,
        "pipeline": pipeline,
        "history": history,
        "lemonade": {
            "available": lemonade_available,
            "url": lemonade_url,
            "model": lemonade_model,
            "backend": lemonade_backend,
            "inferencing": any(
                "Lemonade AI" in v.get("task", "") or "⚡" in v.get("task", "")
                for v in active.values()
            ),
        },
        "schedules": schedules,
        "activity_seq": get_tracker().latest_seq(),
    })


@router.post("/agents/scan-now")
async def scan_now(request: Request) -> RedirectResponse:
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)
    from snap_dashboard.agents.release_scanner import ReleaseScannerAgent
    from snap_dashboard.agents.runner import get_runner
    get_runner().submit(ReleaseScannerAgent(user_id=user["id"]))
    return RedirectResponse(url="/agents", status_code=303)


@router.post("/agents/test-lemonade")
async def test_lemonade(request: Request) -> JSONResponse:
    """Ping lemonade-server (starting the embedded instance if needed) and return availability + model list."""
    user = get_current_user(request)
    if user is None:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    uc = get_user_config(user["id"])
    try:
        import asyncio

        import httpx

        from snap_dashboard.lemonade.client import get_lemonade_client

        def _probe():
            client = get_lemonade_client(uc, ensure_started=True)
            available = client.is_available() if client else False
            models: list[str] = []
            if client and available:
                with httpx.Client(timeout=5) as hc:
                    resp = hc.get(f"{client.base_url}/v1/models", headers=client._headers())
                if resp.status_code == 200:
                    models = [m.get("id", "") for m in resp.json().get("data", [])]
            return client, available, models

        client, available, models = await asyncio.to_thread(_probe)
        return JSONResponse({
            "available": available,
            "url": client.base_url if client else "",
            "model": client.model if client else "",
            "models": models,
        })
    except Exception as exc:
        return JSONResponse({"available": False, "error": str(exc)})


# ---------------------------------------------------------------------------
# SSE — activity stream
# ---------------------------------------------------------------------------

@router.get("/api/events")
async def sse_events(request: Request):
    """Server-Sent Events stream for live agent activity and version-bump updates.

    Agent activity comes from the in-memory ActivityTracker (zero DB reads).
    Version-bump status changes are detected by comparing updated_at against
    the last poll timestamp, so *any* transition through the pipeline triggers
    an event — not just newly created PRs.
    """
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]

    async def event_generator():
        import asyncio
        from snap_dashboard.agents.runner import get_tracker
        tracker = get_tracker()
        last_seq = tracker.latest_seq()
        # Track last poll time so we stream status changes on existing PRs.
        last_poll_ts = datetime.now(timezone.utc)

        try:
            while True:
                if await request.is_disconnected():
                    break

                # Agent activity log entries (in-memory, no DB)
                # Advance the cursor past other users' entries too, even
                # though they're filtered out of what this stream sends.
                seq_now = tracker.latest_seq()
                for entry in tracker.get_log_since(last_seq, user_id):
                    if entry["seq"] > seq_now:
                        break
                    yield f"event: agent_activity\ndata: {json.dumps(entry)}\n\n"
                last_seq = seq_now

                # Version bump status changes since last poll.
                # Using updated_at means both new PRs *and* status transitions
                # on existing PRs are streamed in real time.
                current_ts = datetime.now(timezone.utc)
                with get_session() as session:
                    changed_bumps = (
                        session.query(VersionBumpPR)
                        .filter(
                            VersionBumpPR.user_id == user_id,
                            VersionBumpPR.updated_at >= last_poll_ts,
                        )
                        .order_by(VersionBumpPR.updated_at)
                        .limit(20)
                        .all()
                    )
                    for b in changed_bumps:
                        snap_name = b.snap.name if b.snap else ""
                        payload = json.dumps({
                            "id": b.id,
                            "snap_name": snap_name,
                            "old_version": b.old_version or "",
                            "new_version": b.new_version or "",
                            "status": b.status,
                            "pr_url": b.bot_pr_url or "",
                        })
                        yield f"event: version_bump_update\ndata: {payload}\n\n"
                last_poll_ts = current_ts

                yield ": keepalive\n\n"
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
