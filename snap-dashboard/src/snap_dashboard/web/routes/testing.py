"""Testing routes — YARF test orchestration and promotion."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from snap_dashboard.config import get_config
from snap_dashboard.db.models import TestRun
from snap_dashboard.db.session import get_session
from snap_dashboard.testing.orchestrator import (
    find_snaps_needing_tests,
    poll_for_gh_run_id,
    suite_exists_in_repo,
    sync_test_runs,
    trigger_workflow,
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
    config = get_config()

    with get_session() as session:
        snaps_needing_raw = find_snaps_needing_tests(session)

        # Annotate each item with suite existence and any active run,
        # and replace the ORM Snap object with a plain dict to avoid
        # DetachedInstanceError after the session closes.
        snaps_needing = []
        for item in snaps_needing_raw:
            snap_name = item["snap"].name
            existing = (
                session.query(TestRun)
                .filter_by(
                    snap_name=snap_name,
                    version=item["version"],
                    promoted=False,
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
                }
                if existing
                else None
            )
            snaps_needing.append(
                {
                    "snap": {"name": snap_name},
                    "from_channel": item["from_channel"],
                    "version": item["version"],
                    "revision": item["revision"],
                    "stable_ver": item["stable_ver"],
                    "can_promote": item["can_promote"],
                    "has_suite": suite_exists_in_repo(
                        config.testing_repo, snap_name, config.github_token
                    ),
                    "existing_run": existing_run,
                }
            )

        all_runs = (
            session.query(TestRun)
            .order_by(TestRun.started_at.desc())
            .limit(50)
            .all()
        )
        # Detach data we need outside the session
        runs_data = [
            {
                "id": r.id,
                "snap_name": r.snap_name,
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
            }
            for r in all_runs
        ]

    pending_promotion = [
        r for r in runs_data
        if r["status"] == "passed" and not r["promoted"] and r["from_channel"] == "candidate"
    ]

    return templates.TemplateResponse(
        "testing.html",
        {
            "request": request,
            "config": config,
            "snaps_needing": snaps_needing,
            "all_runs": runs_data,
            "pending_promotion": pending_promotion,
            "last_run": None,
        },
    )


# ---------------------------------------------------------------------------
# Trigger a test workflow
# ---------------------------------------------------------------------------


@router.post("/testing/trigger/{snap_name}")
async def trigger_test(
    snap_name: str,
    request: Request,
    background_tasks: BackgroundTasks,
    from_channel: str = Form(default="candidate"),
    version: str = Form(default=""),
    revision: str = Form(default="0"),
) -> RedirectResponse:
    """Dispatch a YARF workflow for *snap_name* and redirect to the testing page."""
    rev: int | None = int(revision) if revision.isdigit() and int(revision) > 0 else None
    triggered_at = datetime.now(timezone.utc)

    def _bg() -> None:
        ok, err, db_run_id = trigger_workflow(
            snap_name, from_channel, version, rev, triggered_by="manual"
        )
        if not ok:
            logger.error("Failed to trigger test for %s: %s", snap_name, err)
            return
        if db_run_id:
            poll_for_gh_run_id(db_run_id, triggered_at)

    background_tasks.add_task(_bg)
    return RedirectResponse(url="/testing", status_code=303)


# ---------------------------------------------------------------------------
# Sync run statuses from GitHub
# ---------------------------------------------------------------------------


@router.post("/testing/sync")
async def sync_runs(background_tasks: BackgroundTasks) -> RedirectResponse:
    """Sync test run statuses from GitHub PRs in the background."""
    background_tasks.add_task(sync_test_runs)
    return RedirectResponse(url="/testing", status_code=303)


# ---------------------------------------------------------------------------
# Mark a run as failed (manual override for stuck runs)
# ---------------------------------------------------------------------------


@router.post("/testing/runs/{run_id}/fail")
async def mark_run_failed(run_id: int) -> RedirectResponse:
    """Manually mark an in-flight run as failed."""
    with get_session() as session:
        run = session.query(TestRun).get(run_id)
        if run and run.status not in ("passed", "promoted"):
            run.status = "failed"
            run.finished_at = datetime.now(timezone.utc)
    return RedirectResponse(url="/testing", status_code=303)


# ---------------------------------------------------------------------------
# Live status API — polled by the testing page JS
# ---------------------------------------------------------------------------


@router.get("/testing/api/status")
async def testing_status() -> JSONResponse:
    """Return current status of in-flight and recently finished test runs."""
    config = get_config()
    with get_session() as session:
        runs = (
            session.query(TestRun)
            .order_by(TestRun.started_at.desc())
            .limit(50)
            .all()
        )
        data = [
            {
                "id": r.id,
                "status": r.status,
                "gh_run_id": r.gh_run_id,
                "pr_number": r.pr_number,
                "pr_url": r.pr_url,
            }
            for r in runs
        ]
    return JSONResponse(
        {
            "runs": data,
            "testing_repo": config.testing_repo or "",
        }
    )


# ---------------------------------------------------------------------------
# PR detail page
# ---------------------------------------------------------------------------


@router.get("/testing/pr/{snap_name}/{pr_number}", response_class=HTMLResponse)
async def view_pr(snap_name: str, pr_number: int, request: Request) -> HTMLResponse:
    """Render the PR detail page for a test run."""
    config = get_config()

    from snap_dashboard.github.pr_viewer import (
        get_pr_details,
        get_pr_screenshot_urls,
        parse_pr_metadata,
    )

    pr_data: dict = {}
    screenshot_urls: list[str] = []
    metadata: dict = {}

    if config.testing_repo:
        pr_data = get_pr_details(config.testing_repo, pr_number, config.github_token)
        metadata = pr_data.get("metadata", {})
        screenshot_urls = get_pr_screenshot_urls(
            config.testing_repo, pr_data, "", config.github_token
        )

    with get_session() as session:
        run_orm = (
            session.query(TestRun)
            .filter_by(snap_name=snap_name, pr_number=pr_number)
            .first()
        )
        if run_orm:
            run_dict = {
                "id": run_orm.id,
                "snap_name": run_orm.snap_name,
                "pr_number": run_orm.pr_number,
                "status": run_orm.status,
                "version": run_orm.version,
                "from_channel": run_orm.from_channel,
                "revision": run_orm.revision,
                "promoted": run_orm.promoted,
            }
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
        f"https://github.com/{config.testing_repo}/pull/{pr_number}",
    )

    return templates.TemplateResponse(
        "pr_detail.html",
        {
            "request": request,
            "run": run_dict,
            "pr": pr_info,
            "pr_url": pr_url,
            "metadata": metadata,
            "screenshot_urls": screenshot_urls,
            "files": pr_data.get("files", []),
            "comments": pr_data.get("comments", []),
            "testing_repo": config.testing_repo,
            "error": None,
            "last_run": None,
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
) -> HTMLResponse | RedirectResponse:
    """Promote a snap revision to stable via ``snapcraft release`` then close the test PR."""
    from snap_dashboard.testing.promoter import close_test_pr, promote_snap

    config = get_config()
    ok, output = promote_snap(snap_name, revision, to_channel)

    version = ""
    with get_session() as session:
        run_orm = (
            session.query(TestRun)
            .filter_by(snap_name=snap_name, pr_number=pr_number)
            .first()
        )
        if ok:
            if run_orm:
                version = run_orm.version or ""
                run_orm.status = "promoted"
                run_orm.promoted = True
                run_orm.promoted_at = datetime.now(timezone.utc)
        else:
            if run_orm:
                run_orm.error_msg = output[:500]

    if ok:
        if config.testing_repo:
            close_test_pr(
                config.testing_repo,
                pr_number,
                snap_name,
                version,
                config.github_token,
            )
        return RedirectResponse(url="/testing", status_code=303)

    # Render the detail page again with an error message
    return templates.TemplateResponse(
        "pr_detail.html",
        {
            "request": request,
            "run": {
                "snap_name": snap_name,
                "pr_number": pr_number,
                "status": "error",
                "revision": revision,
                "promoted": False,
            },
            "pr": {},
            "pr_url": f"https://github.com/{config.testing_repo}/pull/{pr_number}",
            "metadata": {},
            "screenshot_urls": [],
            "files": [],
            "comments": [],
            "testing_repo": config.testing_repo,
            "error": output,
            "last_run": None,
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
