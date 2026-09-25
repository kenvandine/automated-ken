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
from snap_dashboard.db.models import PromotionDismissal, Snap, TestRun
from snap_dashboard.db.session import get_session
from snap_dashboard.testing.orchestrator import (
    find_snaps_needing_tests,
    suite_exists_in_repo,
    sync_test_runs,
    trigger_remote_run,
)
from snap_dashboard.testing.release_set import (
    PROMOTED,
    READY,
    candidate_release_set,
    describe,
    member_state,
    promote_release_set,
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
            # One existing run per architecture in this group, so the row
            # can show "amd64: passed, arm64: pending" instead of only ever
            # reflecting one architecture's state.
            per_arch_runs = {}
            for arch in item["architectures"]:
                existing = (
                    session.query(TestRun)
                    .filter_by(
                        snap_name=snap_name,
                        architecture=arch,
                        version=item["version"],
                        from_channel=item["from_channel"],
                        user_id=user_id,
                    )
                    .order_by(TestRun.started_at.desc())
                    .first()
                )
                if existing:
                    per_arch_runs[arch] = {
                        "id": existing.id,
                        "status": existing.status,
                        "gh_run_id": existing.gh_run_id,
                        "pr_number": existing.pr_number,
                        "pr_url": existing.pr_url,
                        "repo": existing.repo or uc.testing_repo,
                        "has_log": bool(existing.log_output),
                        "review_decision": existing.review_decision,
                        "review_confidence": existing.review_confidence,
                    }
            # Candidate rows can be promoted to stable — as a set; see
            # testing/release_set.py and promote_set() below.
            promote_ready: list[str] = []
            promote_blocked: list[str] = []
            if item["can_promote"]:
                for member in candidate_release_set(session, user_id, snap_name, item["version"]):
                    state = member_state(member)
                    if state == READY:
                        promote_ready.append(member["architecture"])
                    elif state != PROMOTED:
                        promote_blocked.append(describe(member, state))
            prepared.append(
                {
                    "snap": {"name": snap_name},
                    "promote_ready": promote_ready,
                    "promote_blocked": promote_blocked,
                    "architectures": item["architectures"],
                    "from_channel": item["from_channel"],
                    "version": item["version"],
                    "revisions": item["revisions"],
                    "stable_ver": item["stable_ver"],
                    "can_promote": item["can_promote"],
                    "packaging_repo": item["snap"].packaging_repo,
                    "existing_runs": per_arch_runs,
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
                "review_decision": r.review_decision,
                "review_confidence": r.review_confidence,
            }
            for r in all_runs
        ]

        # One card per candidate release set (snap + version) with at least
        # one passed, un-promoted architecture — promoted as a set. Only
        # for snaps still tracked (TestRun rows outlive a removed Snap —
        # see settings.settings_remove_snap — and this is keyed by the
        # snap_name string, not a live FK, so it needs an explicit check)
        # and not explicitly dismissed for this exact version.
        tracked_snap_names = {
            s.name for s in session.query(Snap.name).filter_by(user_id=user_id).all()
        }
        dismissed = {
            (d.snap_name, d.version)
            for d in session.query(PromotionDismissal).filter_by(user_id=user_id).all()
        }
        pending_promotion = []
        seen: set[tuple[str, str]] = set()
        for r in runs_data:
            key = (r["snap_name"], r["version"] or "")
            if (
                r["status"] != "passed" or r["promoted"] or r["from_channel"] != "candidate"
                or not r["version"] or key in seen
                or r["snap_name"] not in tracked_snap_names
                or key in dismissed
            ):
                continue
            seen.add(key)
            members = []
            for member in candidate_release_set(session, user_id, r["snap_name"], r["version"]):
                run = member["run"]
                members.append(
                    {
                        "architecture": member["architecture"],
                        "state": member_state(member),
                        "run_id": run.id if run else None,
                        "review_decision": run.review_decision if run else None,
                        "review_confidence": run.review_confidence if run else None,
                    }
                )
            pending_promotion.append(
                {"snap_name": r["snap_name"], "version": r["version"], "members": members}
            )

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
        # A YARF suite lives in the packaging repo and doesn't vary by
        # architecture, so check each snap once — not once per architecture
        # in its group — deduplicated by name.
        items_by_name = {
            item["snap"].name: {
                "snap_name": item["snap"].name,
                "packaging_repo": item["snap"].packaging_repo,
            }
            for item in snaps_needing_raw
        }

    def _check_all() -> list[dict]:
        results = []
        for item in items_by_name.values():
            has_suite = suite_exists_in_repo(
                uc.testing_repo, item["snap_name"], uc.github_token,
                packaging_repo=item["packaging_repo"],
            )
            results.append({"snap_name": item["snap_name"], "has_suite": has_suite})
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
        skip_if_exists=True,
    )
    if not ok:
        logger.error("Failed to trigger test for %s: %s", snap_name, err)

    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse(
            {"ok": ok, "error": err, "run_id": db_run_id, "status": "pending"},
            status_code=200 if ok else 400,
        )
    return RedirectResponse(url="/testing", status_code=303)


@router.post("/testing/trigger-group/{snap_name}")
async def trigger_test_group(
    snap_name: str,
    request: Request,
    from_channel: str = Form(default="candidate"),
    version: str = Form(default=""),
    architecture: list[str] = Form(default=[]),
    revision: list[str] = Form(default=[]),
) -> JSONResponse:
    """Queue one YARF test run per architecture for a "needs testing" row.

    The "Needs Testing" table groups every testable architecture a version
    covers into one row (see ``orchestrator.find_snaps_needing_tests``), so
    one "Run Tests" click here queues one run per architecture instead of
    requiring a separate click per arch. ``architecture``/``revision`` are
    parallel lists (same order, one hidden input pair per arch — see
    testing.html) so each run gets its own recorded revision.
    """
    user = get_current_user(request)
    if user is None:
        return JSONResponse({"error": "not authenticated"}, status_code=401)

    user_id = user["id"]
    results = []
    for i, arch in enumerate(architecture):
        rev_str = revision[i] if i < len(revision) else ""
        rev: int | None = int(rev_str) if rev_str.isdigit() and int(rev_str) > 0 else None
        ok, err, db_run_id = trigger_remote_run(
            snap_name, from_channel, version, rev,
            architecture=arch,
            triggered_by="manual",
            user_id=user_id,
            skip_if_exists=True,
        )
        if not ok:
            logger.error("Failed to trigger test for %s (%s): %s", snap_name, arch, err)
        results.append({"architecture": arch, "ok": ok, "error": err, "run_id": db_run_id, "status": "pending"})

    return JSONResponse({"ok": all(r["ok"] for r in results), "results": results})


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
# Shared screenshot-comparison + review-score context
# ---------------------------------------------------------------------------


def _build_review_context(
    user_id: int | None,
    snap_name: str,
    architecture: str,
    pr_number: int | None,
    test_run_id: int,
    review_decision: str | None,
    review_confidence: float | None,
    review_reasoning: str | None,
    effective_repo: str,
    uc,
) -> dict:
    """Build the AI-review + baseline-vs-new screenshot context for a TestRun.

    Shared by the run-detail page and the PR detail page so "ready to
    promote" is consistently explorable (review decision/confidence/
    reasoning, and a side-by-side comparison against the last known-good
    stable screenshot) no matter which page a user lands on. Takes plain
    values rather than the ORM row so callers can extract everything they
    need while their session is still open, then call this afterwards
    (baseline/screenshot loading can make GitHub API requests).
    """
    from snap_dashboard.testing.baselines import (
        get_or_build_stable_baseline_assets,
        load_test_run_screenshots,
        pair_screenshots,
    )

    baseline_assets = get_or_build_stable_baseline_assets(
        user_id, snap_name, architecture, effective_repo, uc.github_token if uc else "",
    )
    new_assets = load_test_run_screenshots(
        effective_repo, pr_number, uc.github_token if uc else "", test_run_id=test_run_id,
    )
    pairs = pair_screenshots(baseline_assets, new_assets)
    paired_new_names = {new.image_name for _baseline, new in pairs}
    unpaired_new = [a for a in new_assets if a.image_name not in paired_new_names]

    return {
        "review_decision": review_decision,
        "review_confidence": review_confidence,
        "review_reasoning": review_reasoning,
        "screenshot_pairs": pairs,
        "unpaired_screenshots": unpaired_new,
        "has_baseline": bool(baseline_assets),
    }


# ---------------------------------------------------------------------------
# Run detail page — canonical link target for any test run, PR or not
# ---------------------------------------------------------------------------


@router.get("/testing/runs/{run_id}", response_class=HTMLResponse)
def view_run(run_id: int, request: Request) -> HTMLResponse:
    """Render a self-contained detail page for a single test run.

    Unlike ``/testing/pr/{snap_name}/{pr_number}`` (which needs a GitHub PR
    to look up), this works for every run — including ones with no
    associated PR (e.g. a manually-triggered smoke test) — since it's the
    only reliable place to view a run's screenshots and AI review score.
    Redirects to the PR detail page instead when one exists, since that
    page has additional GitHub-sourced context (files/comments/PR body).
    """
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]
    with get_session() as session:
        run_orm = session.query(TestRun).filter_by(id=run_id, user_id=user_id).first()
        if run_orm is None:
            return PlainTextResponse("Run not found", status_code=404)
        if run_orm.pr_number:
            return RedirectResponse(
                url=f"/testing/pr/{run_orm.snap_name}/{run_orm.pr_number}", status_code=303,
            )
        run_data = {
            "id": run_orm.id,
            "snap_name": run_orm.snap_name,
            "architecture": run_orm.architecture or "amd64",
            "from_channel": run_orm.from_channel,
            "version": run_orm.version,
            "revision": run_orm.revision,
            "status": run_orm.status,
            "promoted": run_orm.promoted,
            "error_msg": run_orm.error_msg,
            "started_at": run_orm.started_at,
            "finished_at": run_orm.finished_at,
            "repo": run_orm.repo,
            "has_log": bool(run_orm.log_output),
        }
        review_decision = run_orm.review_decision
        review_confidence = run_orm.review_confidence
        review_reasoning = run_orm.review_reasoning

        # A candidate run is one architecture of a release set; show the
        # whole set and promote it together (see testing/release_set.py).
        release_set = []
        if run_orm.from_channel == "candidate" and run_orm.version:
            for member in candidate_release_set(session, user_id, run_orm.snap_name, run_orm.version):
                release_set.append(
                    {
                        "architecture": member["architecture"],
                        "run_id": member["run"].id if member["run"] else None,
                        "revision": member["revision"],
                        "state": member_state(member),
                    }
                )

    uc = get_user_config(user_id)
    effective_repo = run_data["repo"] or uc.testing_repo
    context = _build_review_context(
        user_id, run_data["snap_name"], run_data["architecture"], None, run_data["id"],
        review_decision, review_confidence, review_reasoning, effective_repo, uc,
    )

    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {
            "run": run_data,
            "release_set": release_set,
            "current_user": user,
            "last_run": None,
            **context,
        },
    )


# ---------------------------------------------------------------------------
# PR detail page
# ---------------------------------------------------------------------------


@router.get("/testing/pr/{snap_name}/{pr_number}", response_class=HTMLResponse)
def view_pr(snap_name: str, pr_number: int, request: Request) -> HTMLResponse:
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
                "architecture": run_orm.architecture or "amd64",
                "pr_number": run_orm.pr_number,
                "status": run_orm.status,
                "version": run_orm.version,
                "from_channel": run_orm.from_channel,
                "revision": run_orm.revision,
                "promoted": run_orm.promoted,
                "repo": run_orm.repo,
                "review_decision": run_orm.review_decision,
                "review_confidence": run_orm.review_confidence,
                "review_reasoning": run_orm.review_reasoning,
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
            "architecture": "amd64",
            "pr_number": pr_number,
            "status": metadata.get("status", "unknown"),
            "version": metadata.get("version", ""),
            "from_channel": metadata.get("from_channel", ""),
            "revision": metadata.get("revision"),
            "promoted": False,
            "review_decision": None,
            "review_confidence": None,
            "review_reasoning": None,
        }

    review_context: dict = {}
    if run_dict.get("id"):
        review_context = _build_review_context(
            user_id, run_dict["snap_name"], run_dict["architecture"], pr_number, run_dict["id"],
            run_dict["review_decision"], run_dict["review_confidence"], run_dict["review_reasoning"],
            effective_repo, uc,
        )

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
            **review_context,
            "current_user": user,
        },
    )


# ---------------------------------------------------------------------------
# Promote a snap to stable
# ---------------------------------------------------------------------------


@router.post("/testing/promote-set/{snap_name}")
def promote_set(
    snap_name: str,
    request: Request,
    version: str = Form(...),
    override: str = Form(default=""),
    return_to: str = Form(default="/testing"),
) -> RedirectResponse:
    """Promote every ready architecture of a candidate version to stable, together.

    An architecture is ready when its latest candidate run for this
    version passed and has a revision. If any architecture isn't ready
    (untested, still running, failed…) the request is refused unless
    ``override`` is set — only the confirmed "Promote (override)" buttons
    send it, after naming the architectures that will be left out — and
    the override is recorded on every promoted run.
    """
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)
    user_id = user["id"]
    if not return_to.startswith(("/testing", "/version-bumps")):
        return_to = "/testing"

    with get_session() as session:
        states = [
            (m, member_state(m))
            for m in candidate_release_set(session, user_id, snap_name, version)
        ]
        ready_ids = [m["run"].id for m, st in states if st == READY]
        blocked = [describe(m, st) for m, st in states if st not in (READY, PROMOTED)]

    if not ready_ids:
        logger.info("promote_set: nothing ready for %s %s (%s)", snap_name, version, "; ".join(blocked))
        return RedirectResponse(url=return_to, status_code=303)
    if blocked and not override:
        logger.warning(
            "promote_set: refusing partial promotion of %s %s without override (blocked: %s)",
            snap_name, version, "; ".join(blocked),
        )
        return RedirectResponse(url=return_to, status_code=303)

    promote_release_set(
        user_id, snap_name, version, ready_ids, get_user_config(user_id),
        skipped=blocked if override else None,
    )
    return RedirectResponse(url=return_to, status_code=303)


@router.post("/testing/dismiss-promotion")
def dismiss_promotion(
    request: Request,
    snap_name: str = Form(...),
    version: str = Form(...),
) -> RedirectResponse:
    """Hide a Pending Promotion card for this exact (snap, version) build.

    Purely a per-user "not now" — it doesn't touch the underlying
    TestRuns, so the release set can still be promoted manually via its
    run-detail page, and a later candidate *version* for the same snap
    will show its own card as usual (see PromotionDismissal docstring).
    """
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)
    user_id = user["id"]

    with get_session() as session:
        existing = (
            session.query(PromotionDismissal)
            .filter_by(user_id=user_id, snap_name=snap_name, version=version)
            .first()
        )
        if existing is None:
            session.add(
                PromotionDismissal(user_id=user_id, snap_name=snap_name, version=version)
            )
    return RedirectResponse(url="/testing", status_code=303)


@router.post("/testing/promote/{snap_name}", response_model=None)
def promote_snap_route(
    snap_name: str,
    request: Request,
    pr_number: int = Form(default=0),
    revision: int = Form(...),
    to_channel: str = Form(default="stable"),
    run_id_field: int | None = Form(default=None, alias="run_id"),
) -> HTMLResponse:
    """Promote a snap revision to stable via ``snapcraft release`` then close the test PR.

    Looked up by ``run_id`` when given (works for any run, PR or not — see
    ``run_detail.html``) and falls back to the legacy ``(snap_name, pr_number)``
    lookup otherwise (``pr_detail.html``, and older Pending Promotion cards).
    """
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
    run_pr_number = pr_number
    effective_repo = uc.testing_repo
    with get_session() as session:
        if run_id_field:
            run_orm = session.query(TestRun).filter_by(id=run_id_field, user_id=user_id).first()
        else:
            run_orm = (
                session.query(TestRun)
                .filter_by(snap_name=snap_name, pr_number=pr_number, user_id=user_id)
                .first()
            )
        if run_orm and run_orm.repo:
            effective_repo = run_orm.repo
        if run_orm:
            run_pr_number = run_orm.pr_number or 0
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
        if effective_repo and run_pr_number:
            close_test_pr(
                effective_repo,
                run_pr_number,
                snap_name,
                version,
                uc.github_token,
            )
        return RedirectResponse(url="/testing", status_code=303)

    # Failed: for a run_id-based (no PR) promote, redirect back to its
    # run-detail page — run_orm.error_msg was already persisted above and
    # that page renders it. Otherwise render the legacy PR detail page
    # inline with the error, since it needs a live pr_number to build a
    # sensible pr_url.
    if run_id_field:
        return RedirectResponse(url=f"/testing/runs/{run_id_field}", status_code=303)

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
