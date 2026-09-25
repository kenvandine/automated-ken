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
from fastapi.responses import JSONResponse, Response

from snap_dashboard.auth import get_user_config
from snap_dashboard.db.models import Runner, Snap, TestRun, TestRunScreenshot
from snap_dashboard.db.session import get_session
from snap_dashboard.runners import generate_token, hash_token
from snap_dashboard.testing.orchestrator import submit_test_run_failure_analysis, submit_test_run_reviewer
from snap_dashboard.testing.suite_zip import list_suite_files

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/runners")

# Cap on stored per-job debug log size (~500KB) so a runaway/looping test
# suite can't bloat the sqlite DB indefinitely.
_MAX_LOG_CHARS = 500_000

_NEXT_JOB_POLL_INTERVAL = 1.0
_DEFAULT_NEXT_JOB_TIMEOUT = 25


def _client_ip(request: Request) -> str | None:
    """Best-effort caller IP, purely informational (e.g. "ssh in to debug").

    Not trusted for auth — enrollment/heartbeat still require the bearer
    secret regardless. Prefers X-Forwarded-For (set by uvicorn's
    ProxyHeadersMiddleware, already enabled by default for
    ``forwarded_allow_ips``) so a runner behind a reverse proxy still
    reports its real address; falls back to the direct TCP peer.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


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
        runner.ip_address = _client_ip(request)
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
    arch = body.get("arch", "")

    cancel_requested = False
    with get_session() as session:
        runner = session.query(Runner).get(runner_id)
        if runner is None:
            return JSONResponse({"error": "runner not found"}, status_code=404)
        runner.status = "locked" if locked else status
        runner.idle_seconds = idle_seconds
        runner.last_heartbeat_at = datetime.now(timezone.utc)
        runner.ip_address = _client_ip(request)
        # Self-heal runners enrolled before arch reporting existed (or
        # whose reported arch has since changed) without requiring a
        # manual re-enrollment — see automated_ken_runner.runner._maybe_heartbeat.
        if arch and runner.arch != arch:
            runner.arch = arch
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
    """Atomically claim the highest-priority queued job this runner can actually run.

    Only jobs belonging to the runner's own user are ever eligible — a
    runner fetches the job's suite with its owner's GitHub token, so it
    must never pick up someone else's work. A job explicitly pre-assigned
    to this runner (``TestRun.runner_id``, set by an operator picking a
    specific machine from the dashboard) is always eligible — that's a
    deliberate human override. An unassigned job is only eligible if its
    target architecture matches this runner's reported arch (see
    ``Runner.arch`` / ``automated_ken_runner.arch``), so e.g. an arm64 job
    never gets picked up by an amd64 runner and vice versa. A job with no
    recorded architecture, or a runner with no reported arch yet, is
    treated as "amd64" for matching purposes — the long-standing default
    before per-arch dispatch existed.
    """
    with get_session() as session:
        runner = session.query(Runner).get(runner_id)
        if runner is None or runner.revoked_at is not None:
            return None
        if runner.current_test_run_id is not None:
            return None  # already has a job in flight

        runner_arch = (runner.arch or "amd64").strip().lower()
        candidates = (
            session.query(TestRun)
            .filter_by(dispatch_target="remote_runner", status="pending", user_id=runner.user_id)
            .filter((TestRun.runner_id == runner_id) | (TestRun.runner_id.is_(None)))
            .order_by(TestRun.priority.desc(), TestRun.started_at.asc())
            .all()
        )
        candidate = next(
            (
                c for c in candidates
                if c.runner_id == runner_id
                or (c.architecture or "amd64").strip().lower() == runner_arch
            ),
            None,
        )
        if candidate is None:
            return None

        candidate.status = "triggered"
        candidate.runner_id = runner_id
        # started_at doubles as the queue timestamp while pending; reset it
        # at claim time so the watchdog's timeout (agents/runner_watchdog.py)
        # measures actual run time, not how long the job waited for a
        # runner of the right architecture to come free.
        candidate.started_at = datetime.now(timezone.utc)
        runner.current_test_run_id = candidate.id
        runner.status = "busy"
        session.flush()

        uc = get_user_config(candidate.user_id) if candidate.user_id else None
        testing_repo = getattr(uc, "testing_repo", "") if uc else ""
        snap_row = (
            session.query(Snap)
            .filter_by(name=candidate.snap_name, user_id=candidate.user_id)
            .first()
        )

        return {
            "test_run_id": candidate.id,
            "snap_name": candidate.snap_name,
            "channel": candidate.from_channel,
            "architecture": candidate.architecture or "amd64",
            "testing_repo": testing_repo,
            "is_console_app": bool(snap_row.is_console_app) if snap_row else False,
            "is_service": bool(snap_row.is_service) if snap_row else False,
        }


@router.get("/{runner_id}/jobs/{job_id}/suite")
def job_suite(
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
    elif status in ("failed", "error"):
        submit_test_run_failure_analysis(job_id)

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
