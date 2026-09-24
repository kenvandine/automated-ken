"""Bearer-token-authenticated API for remote runner machines.

Runners never accept inbound connections — everything here is a runner
*polling* or *pushing* to us, which is why every endpoint below is a
plain HTTP request/response (no websockets) and safe to run from behind
NAT/home routers.

Auth: every endpoint except ``/enroll`` requires
``Authorization: Bearer <secret>`` matching a non-revoked ``Runner`` row's
``secret_hash``.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
import zipfile
from datetime import datetime, timezone

from fastapi import APIRouter, Header, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse

from snap_dashboard.auth import get_user_config
from snap_dashboard.db.models import Runner, Snap, TestRun, TestRunScreenshot
from snap_dashboard.db.session import get_session
from snap_dashboard.runners import generate_token, hash_token
from snap_dashboard.testing.orchestrator import submit_test_run_reviewer
from snap_dashboard.testing.suite_zip import list_suite_files

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/runners")

# Cap on stored per-job debug log size (~500KB) so a runaway/looping test
# suite can't bloat the sqlite DB indefinitely.
_MAX_LOG_CHARS = 500_000

_NEXT_JOB_POLL_INTERVAL = 1.0
_DEFAULT_NEXT_JOB_TIMEOUT = 25


def _runner_from_bearer(authorization: str | None) -> Runner | None:
    """Resolve a Runner from the ``Authorization: Bearer <secret>`` header."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    secret = authorization[7:].strip()
    if not secret:
        return None
    with get_session() as session:
        runner = (
            session.query(Runner)
            .filter_by(secret_hash=hash_token(secret))
            .filter(Runner.revoked_at.is_(None))
            .first()
        )
        if runner:
            session.expunge(runner)
        return runner


def _auth_or_401(authorization: str | None, runner_id: int) -> Runner | JSONResponse:
    runner = _runner_from_bearer(authorization)
    if runner is None or runner.id != runner_id:
        return JSONResponse({"error": "invalid or revoked runner credentials"}, status_code=401)
    return runner


# ---------------------------------------------------------------------------
# Enrollment
# ---------------------------------------------------------------------------

@router.post("/enroll")
async def enroll(request: Request) -> JSONResponse:
    """Consume a one-time enrollment token and mint a long-lived runner secret."""
    body = await request.json()
    token = body.get("token", "")
    name = body.get("name", "") or "unnamed-runner"
    arch = body.get("arch", "")
    os_name = body.get("os_name", "")
    desktop_env = body.get("desktop_env", "")

    if not token:
        return JSONResponse({"error": "missing token"}, status_code=400)

    token_hash = hash_token(token)
    with get_session() as session:
        runner = (
            session.query(Runner)
            .filter_by(enrollment_token_hash=token_hash)
            .first()
        )
        if runner is None:
            return JSONResponse({"error": "invalid enrollment token"}, status_code=401)
        expires_at = runner.enrollment_expires_at
        if expires_at is not None:
            # SQLite's plain DateTime columns come back naive even though
            # they were written from an aware UTC datetime (see
            # snap_dashboard.runners.effective_status for the same fixup).
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at < datetime.now(timezone.utc):
                return JSONResponse({"error": "enrollment token expired"}, status_code=401)

        secret = generate_token()
        runner.secret_hash = hash_token(secret)
        runner.enrollment_token_hash = None
        runner.enrollment_expires_at = None
        runner.name = name or runner.name
        runner.arch = arch
        runner.os_name = os_name
        runner.desktop_env = desktop_env
        runner.status = "idle"
        session.flush()
        runner_id = runner.id

    logger.info("Runner #%s (%s) enrolled", runner_id, name)
    return JSONResponse({"runner_id": runner_id, "secret": secret})


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

@router.patch("/{runner_id}/heartbeat")
async def heartbeat(
    runner_id: int, request: Request, authorization: str | None = Header(default=None)
) -> JSONResponse:
    result = _auth_or_401(authorization, runner_id)
    if isinstance(result, JSONResponse):
        return result

    body = await request.json()
    status = body.get("status", "idle")
    idle_seconds = body.get("idle_seconds")
    locked = bool(body.get("locked", False))

    cancel_requested = False
    with get_session() as session:
        runner = session.query(Runner).get(runner_id)
        if runner is None:
            return JSONResponse({"error": "runner not found"}, status_code=404)
        runner.status = "locked" if locked else status
        runner.idle_seconds = idle_seconds
        runner.last_heartbeat_at = datetime.now(timezone.utc)
        if runner.current_test_run_id:
            job = session.query(TestRun).get(runner.current_test_run_id)
            if job is not None:
                cancel_requested = bool(job.cancel_requested)

    return JSONResponse({"ok": True, "cancel_requested": cancel_requested})


# ---------------------------------------------------------------------------
# Job queue
# ---------------------------------------------------------------------------

@router.get("/{runner_id}/next-job")
async def next_job(
    runner_id: int,
    timeout: int = _DEFAULT_NEXT_JOB_TIMEOUT,
    authorization: str | None = Header(default=None),
) -> Response:
    """Long-poll for a queued job, returning 204 if nothing shows up in time.

    On server shutdown, uvicorn's ``timeout_graceful_shutdown`` (see
    cli.py) cancels whatever request task is holding this loop open
    rather than waiting out the full ``timeout``, so a `snap stop`/refresh
    isn't held up by an in-flight poll. That surfaces here as an
    ``asyncio.CancelledError`` out of ``asyncio.sleep()`` -- letting it
    propagate produces a scary (but harmless) "Exception in ASGI
    application" traceback + 500 in the journal on every shutdown, so we
    catch it and return a normal empty response instead. The runner
    client already treats any dropped connection the same as a plain
    timeout and just reconnects.
    """
    result = _auth_or_401(authorization, runner_id)
    if isinstance(result, JSONResponse):
        return result

    deadline = time.monotonic() + min(timeout, 60)
    try:
        while True:
            job = _try_claim_job(runner_id)
            if job is not None:
                return JSONResponse(job)
            if time.monotonic() >= deadline:
                return Response(status_code=204)
            await asyncio.sleep(_NEXT_JOB_POLL_INTERVAL)
    except asyncio.CancelledError:
        return Response(status_code=204)


def _try_claim_job(runner_id: int) -> dict | None:
    """Atomically claim the highest-priority queued job for this runner, if any."""
    with get_session() as session:
        runner = session.query(Runner).get(runner_id)
        if runner is None or runner.revoked_at is not None:
            return None
        if runner.current_test_run_id is not None:
            return None  # already has a job in flight

        candidate = (
            session.query(TestRun)
            .filter_by(dispatch_target="remote_runner", status="pending")
            .filter((TestRun.runner_id == runner_id) | (TestRun.runner_id.is_(None)))
            .order_by(TestRun.priority.desc(), TestRun.started_at.asc())
            .first()
        )
        if candidate is None:
            return None

        candidate.status = "triggered"
        candidate.runner_id = runner_id
        runner.current_test_run_id = candidate.id
        runner.status = "busy"
        session.flush()

        uc = get_user_config(candidate.user_id) if candidate.user_id else None
        testing_repo = getattr(uc, "testing_repo", "") if uc else ""

        return {
            "test_run_id": candidate.id,
            "snap_name": candidate.snap_name,
            "channel": candidate.from_channel,
            "architecture": candidate.architecture or "amd64",
            "testing_repo": testing_repo,
        }


@router.get("/{runner_id}/jobs/{job_id}/suite")
async def job_suite(
    runner_id: int, job_id: int, authorization: str | None = Header(default=None)
) -> Response:
    result = _auth_or_401(authorization, runner_id)
    if isinstance(result, JSONResponse):
        return result

    with get_session() as session:
        job = session.query(TestRun).get(job_id)
        if job is None or job.runner_id != runner_id:
            return JSONResponse({"error": "job not found"}, status_code=404)
        snap_name = job.snap_name
        user_id = job.user_id
        job_repo = job.repo
        snap = session.query(Snap).filter_by(name=snap_name, user_id=user_id).first()
        packaging_repo = snap.packaging_repo if snap else None

    uc = get_user_config(user_id) if user_id else None
    testing_repo = getattr(uc, "testing_repo", "") if uc else ""
    token = getattr(uc, "github_token", "") if uc else ""
    if not token:
        from snap_dashboard.config import get_config

        token = get_config().github_token

    if job_repo and packaging_repo and job_repo == packaging_repo:
        # Already resolved to the snap's own colocated repo at dispatch time.
        files = list_suite_files(job_repo, snap_name, token, packaging_repo=job_repo)
    elif job_repo:
        # Already resolved to the legacy shared testing repo at dispatch time.
        files = list_suite_files(job_repo, snap_name, token)
    else:
        files = list_suite_files(testing_repo, snap_name, token, packaging_repo=packaging_repo)

    if not files:
        return JSONResponse({"error": "suite not found"}, status_code=404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, content in files.items():
            zf.writestr(arcname, content)
    return Response(content=buf.getvalue(), media_type="application/zip")


@router.patch("/{runner_id}/jobs/{job_id}")
async def update_job(
    runner_id: int, job_id: int, request: Request, authorization: str | None = Header(default=None)
) -> JSONResponse:
    result = _auth_or_401(authorization, runner_id)
    if isinstance(result, JSONResponse):
        return result

    body = await request.json()
    status = body.get("status")
    error = body.get("error", "")
    log = body.get("log", "")
    terminal = status in ("passed", "failed", "error", "cancelled")

    with get_session() as session:
        job = session.query(TestRun).get(job_id)
        if job is None or job.runner_id != runner_id:
            return JSONResponse({"error": "job not found"}, status_code=404)
        if status:
            job.status = status
        if error:
            job.error_msg = error
        if log:
            # Cap stored size — this is debug output, not something that
            # needs unbounded retention.
            job.log_output = log[-_MAX_LOG_CHARS:]
        if terminal:
            job.finished_at = datetime.now(timezone.utc)
            runner = session.query(Runner).get(runner_id)
            if runner is not None and runner.current_test_run_id == job_id:
                runner.current_test_run_id = None
                runner.status = "idle"

    if status == "passed":
        submit_test_run_reviewer(job_id)

    return JSONResponse({"ok": True})


@router.post("/{runner_id}/jobs/{job_id}/screenshots")
async def upload_screenshot(
    runner_id: int,
    job_id: int,
    file: UploadFile,
    width: int = 0,
    height: int = 0,
    brightness_mean: float = 0.0,
    is_valid: bool = True,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    result = _auth_or_401(authorization, runner_id)
    if isinstance(result, JSONResponse):
        return result

    import base64

    contents = await file.read()

    with get_session() as session:
        job = session.query(TestRun).get(job_id)
        if job is None or job.runner_id != runner_id:
            return JSONResponse({"error": "job not found"}, status_code=404)

        existing = (
            session.query(TestRunScreenshot)
            .filter_by(test_run_id=job_id, image_name=file.filename)
            .first()
        )
        if existing is None:
            session.add(
                TestRunScreenshot(
                    test_run_id=job_id,
                    image_name=file.filename,
                    image_b64=base64.b64encode(contents).decode("ascii"),
                    width=width,
                    height=height,
                    brightness_mean=brightness_mean,
                    is_valid=is_valid,
                    captured_at=datetime.now(timezone.utc),
                )
            )

    return JSONResponse({"ok": True})
