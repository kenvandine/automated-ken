"""Session-auth UI for managing remote runner machines and their job queues."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from snap_dashboard.auth import get_current_user
from snap_dashboard.db.models import Runner, TestRun
from snap_dashboard.db.session import get_session, retry_on_db_lock
from snap_dashboard.runners import effective_status, generate_token, hash_token
from snap_dashboard.web.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()

_ENROLLMENT_TTL_MINUTES = 15


def _runner_dict(runner: Runner) -> dict:
    return {
        "id": runner.id,
        "name": runner.name,
        "status": effective_status(runner),
        "arch": runner.arch or "",
        "os_name": runner.os_name or "",
        "desktop_env": runner.desktop_env or "",
        "idle_seconds": runner.idle_seconds,
        "last_heartbeat_at": (
            runner.last_heartbeat_at.strftime("%H:%M:%S") if runner.last_heartbeat_at else None
        ),
        "current_test_run_id": runner.current_test_run_id,
        "enrolled": runner.secret_hash is not None,
        "pending_enrollment": runner.enrollment_token_hash is not None,
    }


@router.get("/runners", response_class=HTMLResponse)
async def runners_page(request: Request) -> HTMLResponse:
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)
    user_id = user["id"]

    with get_session() as session:
        runners = session.query(Runner).filter_by(user_id=user_id).order_by(Runner.created_at.desc()).all()
        runner_rows = [_runner_dict(r) for r in runners]

        queued = (
            session.query(TestRun)
            .filter_by(dispatch_target="remote_runner")
            .filter(TestRun.status.in_(["pending", "triggered", "running"]))
            .filter(TestRun.user_id == user_id)
            .order_by(TestRun.priority.desc(), TestRun.started_at.asc())
            .all()
        )
        queue_rows = [
            {
                "id": j.id,
                "snap_name": j.snap_name,
                "architecture": j.architecture or "amd64",
                "version": j.version or "",
                "status": j.status,
                "runner_id": j.runner_id,
                "priority": j.priority,
                "started_at": j.started_at.strftime("%H:%M:%S") if j.started_at else "",
                "cancel_requested": j.cancel_requested,
            }
            for j in queued
        ]

        recent = (
            session.query(TestRun)
            .filter_by(dispatch_target="remote_runner", user_id=user_id)
            .order_by(TestRun.started_at.desc())
            .limit(20)
            .all()
        )
        recent_rows = [
            {
                "id": r.id,
                "snap_name": r.snap_name,
                "architecture": r.architecture or "amd64",
                "version": r.version or "",
                "status": r.status,
                "runner_id": r.runner_id,
                "started_at": r.started_at.strftime("%Y-%m-%d %H:%M") if r.started_at else "",
                "duration_s": (
                    int((r.finished_at - r.started_at).total_seconds())
                    if r.finished_at and r.started_at else None
                ),
            }
            for r in recent
        ]

    return templates.TemplateResponse(
        request,
        "runners.html",
        {
            "current_user": user,
            "runners": runner_rows,
            "queue": queue_rows,
            "recent": recent_rows,
        },
    )


@router.post("/runners/new")
async def new_runner(request: Request, name: str = Form(default="")) -> JSONResponse:
    """Create a not-yet-enrolled Runner row and a one-time enrollment token."""
    user = get_current_user(request)
    if user is None:
        return JSONResponse({"error": "not authenticated"}, status_code=401)

    token = generate_token()
    with get_session() as session:
        runner = Runner(
            user_id=user["id"],
            name=name or "pending-enrollment",
            enrollment_token_hash=hash_token(token),
            enrollment_expires_at=datetime.now(timezone.utc) + timedelta(minutes=_ENROLLMENT_TTL_MINUTES),
            status="enrolling",
        )
        session.add(runner)
        session.flush()
        runner_id = runner.id

    server_url = str(request.base_url).rstrip("/")
    command = f"automated-ken-runner enroll --server {server_url} --token {token}"
    return JSONResponse({
        "runner_id": runner_id,
        "token": token,
        "expires_in_minutes": _ENROLLMENT_TTL_MINUTES,
        "command": command,
    })


@router.post("/runners/{runner_id}/rename")
async def rename_runner(runner_id: int, request: Request, name: str = Form(...)) -> RedirectResponse:
    """Change a runner's display name. Purely cosmetic — has no effect on
    auth, enrollment, or job dispatch, which are all keyed by ``id``."""
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)
    name = name.strip()
    if name:
        with get_session() as session:
            runner = session.query(Runner).filter_by(id=runner_id, user_id=user["id"]).first()
            if runner:
                runner.name = name
    return RedirectResponse(url="/runners", status_code=303)


@router.post("/runners/{runner_id}/revoke")
async def revoke_runner(runner_id: int, request: Request) -> RedirectResponse:
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)
    _revoke_runner_db(runner_id, user["id"])
    return RedirectResponse(url="/runners", status_code=303)


@retry_on_db_lock()
def _revoke_runner_db(runner_id: int, user_id: int) -> None:
    with get_session() as session:
        runner = session.query(Runner).filter_by(id=runner_id, user_id=user_id).first()
        if runner:
            runner.revoked_at = datetime.now(timezone.utc)
            runner.secret_hash = None
            if runner.current_test_run_id:
                job = session.query(TestRun).get(runner.current_test_run_id)
                if job and job.status not in ("passed", "failed", "promoted"):
                    job.status = "cancelled"
                    job.cancel_requested = True
                runner.current_test_run_id = None


@router.post("/runners/jobs/{job_id}/cancel")
async def cancel_job(job_id: int, request: Request) -> RedirectResponse:
    """Ask a queued or in-flight remote-runner job to stop.

    Queued jobs are cancelled immediately. In-flight jobs are flagged with
    ``cancel_requested`` — the runner notices on its next heartbeat and
    stops the local YARF process, then reports back a terminal status.
    """
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)
    with get_session() as session:
        job = session.query(TestRun).filter_by(id=job_id, user_id=user["id"]).first()
        if job is None:
            return RedirectResponse(url="/runners", status_code=303)
        if job.status == "pending":
            job.status = "cancelled"
            job.finished_at = datetime.now(timezone.utc)
            if job.runner_id:
                runner = session.query(Runner).get(job.runner_id)
                if runner and runner.current_test_run_id == job_id:
                    runner.current_test_run_id = None
                    runner.status = "idle"
        else:
            job.cancel_requested = True
    return RedirectResponse(url="/runners", status_code=303)


@router.post("/runners/jobs/{job_id}/priority")
async def set_priority(job_id: int, request: Request, delta: int = Form(...)) -> RedirectResponse:
    """Bump a queued job's priority up or down (dashboard reorder buttons)."""
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)
    with get_session() as session:
        job = session.query(TestRun).filter_by(id=job_id, user_id=user["id"]).first()
        if job:
            job.priority += delta
    return RedirectResponse(url="/runners", status_code=303)
