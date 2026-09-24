"""Testing routes — YARF test orchestration and promotion."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from snap_dashboard.auth import get_current_user, get_user_config
from snap_dashboard.db.models import TestRun
from snap_dashboard.db.session import get_session
from snap_dashboard.testing.orchestrator import (
    find_snaps_needing_tests,
    suite_exists_in_repo,
    sync_test_runs,
    trigger_remote_run,
)

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


# ---------------------------------------------------------------------------
# Overview page
# ---------------------------------------------------------------------------


@router.get("/testing", response_class=HTMLResponse)
async def testing_index(request: Request) -> HTMLResponse:
    """Render the YARF testing overview page."""
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]
    uc = get_user_config(user_id)

    with get_session() as session:
        snaps_needing_raw = find_snaps_needing_tests(session, user_id=user_id)

        # Gather plain data only (no network calls) while the session/lock
        # is held. Suite existence is checked lazily by the page's JS via
        # /testing/api/suite-status *after* the page has already rendered
        # — see the comment on that route for why this can no longer be
        # done synchronously here.
        prepared = []
        for item in snaps_needing_raw:
            snap_name = item["snap"].name
            existing = (
                session.query(TestRun)
                .filter_by(
                    snap_name=snap_name,
                    architecture=item["architecture"],
                    version=item["version"],
                    promoted=False,
                    user_id=user_id,
                )
                .order_by(TestRun.started_at.desc())
                .first()
            )
            existing_run = (
                {
                    "id": existing.id,
                    "status": existing.status,
                    "gh_run_id": existing.gh_run_id,
                    "pr_number": existing.pr_number,
                    "pr_url": existing.pr_url,
                    "repo": existing.repo or uc.testing_repo,
                    "has_log": bool(existing.log_output),
                }
                if existing
                else None
            )
            prepared.append(
                {
                    "snap": {"name": snap_name},
                    "architecture": item["architecture"],
                    "from_channel": item["from_channel"],
                    "version": item["version"],
                    "revision": item["revision"],
                    "stable_ver": item["stable_ver"],
                    "can_promote": item["can_promote"],
                    "packaging_repo": item["snap"].packaging_repo,
                    "existing_run": existing_run,
                    # Unknown until /testing/api/suite-status responds —
                    # the template renders a "checking…" placeholder for
                    # None and the page's JS fills this in async.
                    "has_suite": None,
                }
            )

        all_runs = (
            session.query(TestRun)
            .filter_by(user_id=user_id)
            .order_by(TestRun.started_at.desc())
            .limit(50)
            .all()
        )
        # Detach data we need outside the session
        runs_data = [
            {
                "id": r.id,
                "snap_name": r.snap_name,
                "architecture": r.architecture or "amd64",
                "from_channel": r.from_channel,
                "version": r.version,
                "revision": r.revision,
                "status": r.status,
                "gh_run_id": r.gh_run_id,
                "pr_number": r.pr_number,
                "pr_url": r.pr_url,
                "triggered_by": r.triggered_by,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
                "promoted": r.promoted,
                "promoted_at": r.promoted_at,
                "error_msg": r.error_msg,
                "repo": r.repo or uc.testing_repo,
                "has_log": bool(r.log_output),
            }
            for r in all_runs
        ]

    pending_promotion = [
        r for r in runs_data
        if r["status"] == "passed" and not r["promoted"] and r["from_channel"] == "candidate"
    ]

    return templates.TemplateResponse(
        request,
        "testing.html",
        {
            "config": uc,
            "snaps_needing": prepared,
            "all_runs": runs_data,
            "pending_promotion": pending_promotion,
            "last_run": None,
            "current_user": user,
            # Suites are now discovered async (see above) so we can't know
            # this synchronously; assume True whenever there's a testing_repo
            # configured or the user has snaps at all, so the page always
            # renders its normal content and lets the async check settle
            # the per-row detail. The truly-empty-state card is only meant
            # for brand-new setups with nothing configured or recorded yet.
            "any_suite_configured": bool(uc.testing_repo) or bool(prepared) or bool(all_runs),
        },
    )


# ---------------------------------------------------------------------------
# Async suite-existence check — called by the testing page's JS after the
# page has already rendered.
# ---------------------------------------------------------------------------


@router.get("/testing/api/suite-status")
async def suite_status(request: Request) -> JSONResponse:
    """Report which "needs testing" snaps have a YARF suite.

    This used to run synchronously inside the ``/testing`` GET handler,
    which made every page load (and every redirect back to it, e.g. after
    triggering a test) take as long as N sequential GitHub API calls. It's
    also plain ``httpx.Client`` (sync) I/O, which — unlike an awaited async
    HTTP call — blocks this process's *entire* asyncio event loop for its
    duration, stalling every other concurrent request (including runner
    long-polls) the whole time. ``asyncio.to_thread`` moves that blocking
    work off the event loop so the rest of the app stays responsive while
    this endpoint is in flight.
    """
    user = get_current_user(request)
    if user is None:
        return JSONResponse({"results": []}, status_code=401)

    user_id = user["id"]
    uc = get_user_config(user_id)

    with get_session() as session:
        snaps_needing_raw = find_snaps_needing_tests(session, user_id=user_id)
        items = [
            {
                "snap_name": item["snap"].name,
                "architecture": item["architecture"],
                "packaging_repo": item["snap"].packaging_repo,
            }
            for item in snaps_needing_raw
        ]

    def _check_all() -> list[dict]:
        results = []
        for item in items:
            has_suite = suite_exists_in_repo(
                uc.testing_repo, item["snap_name"], uc.github_token,
                packaging_repo=item["packaging_repo"],
            )
            results.append(
                {
                    "snap_name": item["snap_name"],
                    "architecture": item["architecture"],
                    "has_suite": has_suite,
                }
            )
        return results

    results = await asyncio.to_thread(_check_all)
    return JSONResponse({"results": results})


# ---------------------------------------------------------------------------
# Trigger a test workflow
# ---------------------------------------------------------------------------


@router.post("/testing/trigger/{snap_name}", response_model=None)
async def trigger_test(
    snap_name: str,
    request: Request,
    from_channel: str = Form(default="candidate"),
    architecture: str = Form(default="amd64"),
    version: str = Form(default=""),
    revision: str = Form(default="0"),
) -> JSONResponse | RedirectResponse:
    """Queue a YARF test run for *snap_name* on a remote runner.

    ``trigger_remote_run()`` is a pure DB insert (no network calls — see its
    docstring), so it's run synchronously here rather than as a background
    task. Returns JSON so the testing page's JS can update just this one
    row instead of reloading the whole page (which used to re-scan every
    snap and hit GitHub's API for each one — very slow). Falls back to a
    redirect for non-JS/no-Accept-header callers.
    """
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]
    rev: int | None = int(revision) if revision.isdigit() and int(revision) > 0 else None

    # Tests run on a registered remote runner (real hardware polling this
    # dashboard), not GitHub Actions — see snap_dashboard.db.models.Runner.
    ok, err, db_run_id = trigger_remote_run(
        snap_name, from_channel, version, rev,
        architecture=architecture,
        triggered_by="manual",
        user_id=user_id,
    )
    if not ok:
        logger.error("Failed to trigger test for %s: %s", snap_name, err)

    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse(
            {"ok": ok, "error": err, "run_id": db_run_id, "status": "pending"},
            status_code=200 if ok else 400,
        )
    return RedirectResponse(url="/testing", status_code=303)


# ---------------------------------------------------------------------------
# Sync run statuses from GitHub
# ---------------------------------------------------------------------------


@router.post("/testing/sync")
async def sync_runs(
    request: Request,
    background_tasks: BackgroundTasks,
) -> RedirectResponse:
    """Sync test run statuses from GitHub PRs in the background."""
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]
    uc = get_user_config(user_id)
    testing_repo = uc.testing_repo
    github_token = uc.github_token

    background_tasks.add_task(
        sync_test_runs,
        testing_repo=testing_repo,
        github_token=github_token,
        user_id=user_id,
    )
    return RedirectResponse(url="/testing", status_code=303)


# ---------------------------------------------------------------------------
# Runner-captured debug log for a single run
# ---------------------------------------------------------------------------


@router.get("/testing/runs/{run_id}/log", response_class=PlainTextResponse)
async def run_log(run_id: int, request: Request) -> PlainTextResponse:
    """Return the raw stdout/stderr/traceback captured by the remote runner.

    Plain text so it's easy to view in-browser or download/curl, and so we
    don't need any JS/modal plumbing to expose it.
    """
    user = get_current_user(request)
    if user is None:
        return PlainTextResponse("Not authenticated", status_code=401)

    user_id = user["id"]
    with get_session() as session:
        run = session.query(TestRun).filter_by(id=run_id, user_id=user_id).first()
        if run is None:
            return PlainTextResponse("Run not found", status_code=404)
        log = run.log_output or "(no log captured for this run)"

    return PlainTextResponse(log)


# ---------------------------------------------------------------------------
# Mark a run as failed (manual override for stuck runs)
# ---------------------------------------------------------------------------


@router.post("/testing/runs/{run_id}/fail")
async def mark_run_failed(run_id: int, request: Request) -> RedirectResponse:
    """Manually mark an in-flight run as failed."""
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]

    with get_session() as session:
        run = session.query(TestRun).filter_by(id=run_id, user_id=user_id).first()
        if run and run.status not in ("passed", "promoted"):
            run.status = "failed"
            run.finished_at = datetime.now(timezone.utc)
    return RedirectResponse(url="/testing", status_code=303)


# ---------------------------------------------------------------------------
# Live status API — polled by the testing page JS
# ---------------------------------------------------------------------------


@router.get("/testing/api/status")
async def testing_status(request: Request) -> JSONResponse:
    """Return current status of in-flight and recently finished test runs."""
    user = get_current_user(request)
    if user is None:
        return JSONResponse({"runs": [], "testing_repo": ""}, status_code=401)

    user_id = user["id"]
    uc = get_user_config(user_id)

    with get_session() as session:
        runs = (
            session.query(TestRun)
            .filter_by(user_id=user_id)
            .order_by(TestRun.started_at.desc())
            .limit(50)
            .all()
        )
        data = [
            {
                "id": r.id,
                "status": r.status,
                "architecture": r.architecture or "amd64",
                "gh_run_id": r.gh_run_id,
                "pr_number": r.pr_number,
                "pr_url": r.pr_url,
                "repo": r.repo or uc.testing_repo,
                "has_log": bool(r.log_output),
            }
            for r in runs
        ]
    return JSONResponse(
        {
            "runs": data,
            "testing_repo": uc.testing_repo or "",
        }
    )


# ---------------------------------------------------------------------------
# PR detail page
# ---------------------------------------------------------------------------


@router.get("/testing/pr/{snap_name}/{pr_number}", response_class=HTMLResponse)
async def view_pr(snap_name: str, pr_number: int, request: Request) -> HTMLResponse:
    """Render the PR detail page for a test run."""
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]
    uc = get_user_config(user_id)

    from snap_dashboard.github.pr_viewer import (
        get_pr_details,
        get_pr_screenshot_urls,
    )

    pr_data: dict = {}
    screenshot_urls: list[str] = []
    metadata: dict = {}

    with get_session() as session:
        run_orm = (
            session.query(TestRun)
            .filter_by(snap_name=snap_name, pr_number=pr_number, user_id=user_id)
            .first()
        )
        run_data = (
            {
                "id": run_orm.id,
                "snap_name": run_orm.snap_name,
                "pr_number": run_orm.pr_number,
                "status": run_orm.status,
                "version": run_orm.version,
                "from_channel": run_orm.from_channel,
                "revision": run_orm.revision,
                "promoted": run_orm.promoted,
                "repo": run_orm.repo,
            }
            if run_orm
            else None
        )

    # get_pr_details()/get_pr_screenshot_urls() make GitHub API requests —
    # these must run with no session/lock held; see the comment on
    # get_session() for why holding it across network I/O would stall
    # every other request/agent/runner heartbeat in the app.
    effective_repo = (run_data["repo"] if run_data else None) or uc.testing_repo

    if effective_repo:
        pr_data = get_pr_details(effective_repo, pr_number, uc.github_token)
        metadata = pr_data.get("metadata", {})
        screenshot_urls = get_pr_screenshot_urls(
            effective_repo, pr_data, "", uc.github_token
        )

    if run_data:
        run_dict = {k: v for k, v in run_data.items() if k != "repo"}
    else:
        run_dict = {
            "id": None,
            "snap_name": snap_name,
            "pr_number": pr_number,
            "status": metadata.get("status", "unknown"),
            "version": metadata.get("version", ""),
            "from_channel": metadata.get("from_channel", ""),
            "revision": metadata.get("revision"),
            "promoted": False,
        }

    pr_info = pr_data.get("pr", {})
    pr_url = pr_info.get(
        "html_url",
        f"https://github.com/{effective_repo}/pull/{pr_number}",
    )

    return templates.TemplateResponse(
        request,
        "pr_detail.html",
        {
            "run": run_dict,
            "pr": pr_info,
            "pr_url": pr_url,
            "metadata": metadata,
            "screenshot_urls": screenshot_urls,
            "files": pr_data.get("files", []),
            "comments": pr_data.get("comments", []),
            "testing_repo": effective_repo,
            "error": None,
            "last_run": None,
            "current_user": user,
        },
    )


# ---------------------------------------------------------------------------
# Promote a snap to stable
# ---------------------------------------------------------------------------


@router.post("/testing/promote/{snap_name}", response_model=None)
async def promote_snap_route(
    snap_name: str,
    request: Request,
    pr_number: int = Form(...),
    revision: int = Form(...),
    to_channel: str = Form(default="stable"),
) -> HTMLResponse:
    """Promote a snap revision to stable via ``snapcraft release`` then close the test PR."""
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]
    uc = get_user_config(user_id)

    from snap_dashboard.testing.baselines import persist_stable_baseline_for_run
    from snap_dashboard.testing.promoter import close_test_pr, promote_snap

    ok, output = promote_snap(
        snap_name, revision, to_channel,
        store_credentials=getattr(uc, "snapcraft_macaroon", "") or "",
    )

    version = ""
    run_id: int | None = None
    effective_repo = uc.testing_repo
    with get_session() as session:
        run_orm = (
            session.query(TestRun)
            .filter_by(snap_name=snap_name, pr_number=pr_number, user_id=user_id)
            .first()
        )
        if run_orm and run_orm.repo:
            effective_repo = run_orm.repo
        if ok:
            if run_orm:
                run_id = run_orm.id
                version = run_orm.version or ""
                run_orm.status = "promoted"
                run_orm.promoted = True
                run_orm.promoted_at = datetime.now(timezone.utc)
        else:
            if run_orm:
                run_orm.error_msg = output[:500]

    if ok:
        if run_id and effective_repo:
            persist_stable_baseline_for_run(run_id, effective_repo, uc.github_token)
            with get_session() as session:
                from snap_dashboard.db.models import VersionBumpPR

                bump = session.query(VersionBumpPR).filter_by(test_run_id=run_id).first()
                if bump:
                    bump.status = "stable_promoted"
        if effective_repo:
            close_test_pr(
                effective_repo,
                pr_number,
                snap_name,
                version,
                uc.github_token,
            )
        return RedirectResponse(url="/testing", status_code=303)

    # Render the detail page again with an error message
    return templates.TemplateResponse(
        request,
        "pr_detail.html",
        {
            "run": {
                "snap_name": snap_name,
                "pr_number": pr_number,
                "status": "error",
                "revision": revision,
                "promoted": False,
            },
            "pr": {},
            "pr_url": f"https://github.com/{uc.testing_repo}/pull/{pr_number}",
            "metadata": {},
            "screenshot_urls": [],
            "files": [],
            "comments": [],
            "testing_repo": uc.testing_repo,
            "error": output,
            "last_run": None,
            "current_user": user,
        },
    )


# ---------------------------------------------------------------------------
# Workflow template download
# ---------------------------------------------------------------------------


@router.get("/testing/workflow-template")
async def get_workflow_template() -> HTMLResponse:
    """Serve the GitHub Actions workflow YAML template as a downloadable file."""
    from snap_dashboard.testing.workflow_template import WORKFLOW_YAML

    return HTMLResponse(
        content=WORKFLOW_YAML,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=snap-test.yml"},
    )
