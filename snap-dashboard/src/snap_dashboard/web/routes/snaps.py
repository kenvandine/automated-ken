"""Snap management routes — add, view, edit."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from html import escape
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from snap_dashboard.auth import get_current_user, get_user_config
from snap_dashboard.db.models import ChannelMap, CollectionRun, Issue, Snap, TestRun
from snap_dashboard.db.session import get_session
from snap_dashboard.github.repo_discovery import (
    build_packaging_repo_map,
    get_cached_packaging_repo_map,
)
from snap_dashboard.store.client import extract_repo_urls, get_snap_info

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _get_last_run(user_id: int):
    with get_session() as session:
        run = (
            session.query(CollectionRun)
            .filter_by(user_id=user_id, status="success")
            .order_by(CollectionRun.finished_at.desc())
            .first()
        )
        return run.finished_at if run and run.finished_at else None


@router.get("/snaps/add", response_class=HTMLResponse)
async def snap_add_get(request: Request) -> HTMLResponse:
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    return templates.TemplateResponse(
        request,
        "snap_add.html",
        {
            "last_run": _get_last_run(user["id"]),
            "search_result": None,
            "current_user": user,
        },
    )


@router.post("/snaps/search", response_class=HTMLResponse)
def snap_search(
    request: Request,
    snap_name: str = Form(...),
) -> HTMLResponse:
    """Search Snap Store for a snap and return an HTML fragment for HTMX swap.

    Everything interpolated below is escaped: the snap name is user input,
    and the publisher/repo values come from third-party Store metadata.
    """
    if get_current_user(request) is None:
        return HTMLResponse(content="", status_code=401)

    snap_name = snap_name.strip().lower()
    info = get_snap_info(snap_name)

    if not info:
        html = (
            '<div id="search-results" class="search-result not-found">'
            f'<p class="error-msg">Snap <strong>{escape(snap_name)}</strong> not found in the store.</p>'
            "</div>"
        )
        return HTMLResponse(content=html)

    snap_section = info.get("snap", {}) or {}
    publisher_info = snap_section.get("publisher", {}) or {}
    publisher = publisher_info.get("username", "") if isinstance(publisher_info, dict) else ""

    repos = extract_repo_urls(info)
    publisher = escape(publisher)
    packaging_repo = escape(repos.get("packaging_repo") or "")
    upstream_repo = escape(repos.get("upstream_repo") or "")

    html = f"""<div id="search-results" class="search-result found">
  <div class="result-badge">Found in Snap Store</div>
  <div class="form-row">
    <label class="form-label">Publisher</label>
    <input type="text" class="form-input" value="{publisher}" readonly>
  </div>
  <input type="hidden" name="publisher" value="{publisher}">
  <div class="form-row">
    <label class="form-label" for="packaging_repo">Packaging Repository</label>
    <input type="text" class="form-input" id="packaging_repo" name="packaging_repo"
           value="{packaging_repo}" placeholder="https://github.com/owner/repo">
  </div>
  <div class="form-row">
    <label class="form-label" for="upstream_repo">Upstream Repository</label>
    <input type="text" class="form-input" id="upstream_repo" name="upstream_repo"
           value="{upstream_repo}" placeholder="https://github.com/owner/upstream">
  </div>
</div>"""
    return HTMLResponse(content=html)


@router.post("/snaps/add")
async def snap_add_post(
    request: Request,
    snap_name: str = Form(...),
    publisher: str = Form(default=""),
    packaging_repo: str = Form(default=""),
    upstream_repo: str = Form(default=""),
    notes: str = Form(default=""),
) -> RedirectResponse:
    """Save a new snap to the database."""
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]
    snap_name = snap_name.strip().lower()
    uc = get_user_config(user_id)

    with get_session() as session:
        existing = session.query(Snap).filter_by(name=snap_name, user_id=user_id).first()
        if existing:
            return RedirectResponse(url=f"/snap/{snap_name}", status_code=303)

        snap = Snap(
            name=snap_name,
            publisher=publisher or uc.publisher or "",
            manually_added=True,
            packaging_repo=packaging_repo.strip() or None,
            upstream_repo=upstream_repo.strip() or None,
            notes=notes.strip() or None,
            user_id=user_id,
        )
        session.add(snap)

    return RedirectResponse(url=f"/snap/{snap_name}", status_code=303)


@router.get("/snap/{name}", response_class=HTMLResponse)
def snap_detail(request: Request, name: str, background_tasks: BackgroundTasks) -> HTMLResponse:
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]

    with get_session() as session:
        snap = session.query(Snap).filter_by(name=name, user_id=user_id).first()
        if not snap:
            return HTMLResponse(content="<h1>Snap not found</h1>", status_code=404)

        # Channel map grouped by architecture
        cm_rows = (
            session.query(ChannelMap)
            .filter_by(snap_id=snap.id)
            .order_by(ChannelMap.architecture, ChannelMap.channel)
            .all()
        )

        # Issues/PRs
        issues = (
            session.query(Issue)
            .filter_by(snap_id=snap.id)
            .order_by(Issue.updated_at.desc())
            .all()
        )

        # Test run history — TestRun is keyed by snap_name (string), not
        # snap_id, so it's matched the same way the /testing page does.
        test_runs = (
            session.query(TestRun)
            .filter_by(snap_name=snap.name, user_id=user_id)
            .order_by(TestRun.started_at.desc())
            .limit(50)
            .all()
        )

        # Build channel map table: arch -> {channel: {version, revision, released_at}}
        arch_map: dict[str, dict] = {}
        for cm in cm_rows:
            if cm.architecture not in arch_map:
                arch_map[cm.architecture] = {}
            arch_map[cm.architecture][cm.channel] = {
                "version": cm.version,
                "revision": cm.revision,
                "released_at": cm.released_at,
            }

        # Detach objects from session
        snap_data = {
            "id": snap.id,
            "name": snap.name,
            "publisher": snap.publisher,
            "manually_added": snap.manually_added,
            "packaging_repo": snap.packaging_repo,
            "upstream_repo": snap.upstream_repo,
            "notes": snap.notes,
            "is_console_app": snap.is_console_app,
            "created_at": snap.created_at,
            "updated_at": snap.updated_at,
            "packaging_repo_suggested": None,
            "upstream_repo_suggested": None,
        }

        # If either repo URL is unknown, try to suggest one from Snap Store
        issues_data = [
            {
                "id": i.id,
                "issue_number": i.issue_number,
                "title": i.title,
                "state": i.state,
                "type": i.type,
                "url": i.url,
                "author": i.author,
                "created_at": i.created_at,
                "updated_at": i.updated_at,
                "repo_url": i.repo_url,
            }
            for i in issues
        ]

        cm_data = [
            {
                "channel": cm.channel,
                "architecture": cm.architecture,
                "version": cm.version,
                "revision": cm.revision,
                "released_at": cm.released_at,
                "fetched_at": cm.fetched_at,
            }
            for cm in cm_rows
        ]

        test_runs_data = [
            {
                "id": r.id,
                "architecture": r.architecture,
                "from_channel": r.from_channel,
                "version": r.version,
                "revision": r.revision,
                "status": r.status,
                "pr_number": r.pr_number,
                "triggered_by": r.triggered_by,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
                "error_msg": r.error_msg,
                "has_log": bool(r.log_output),
            }
            for r in test_runs
        ]

    # The Snap Store lookup and GitHub repo-map cache check below make
    # network calls (get_snap_info hits the Store API with a 30s timeout).
    # These must run with no session/lock held, since get_session() now
    # serializes all sqlite access process-wide and blocking network I/O
    # under that lock would stall every other request, agent, and runner
    # heartbeat in the app for the duration of the call.
    if not snap_data["packaging_repo"] or not snap_data["upstream_repo"]:
        try:
            info = get_snap_info(snap_data["name"])
        except Exception:
            info = None
        if info:
            repos = extract_repo_urls(info)
            if not snap_data["packaging_repo"]:
                snap_data["packaging_repo_suggested"] = repos.get("packaging_repo")
            if not snap_data["upstream_repo"]:
                snap_data["upstream_repo_suggested"] = repos.get("upstream_repo")

    # Store metadata is often empty for personal snaps (no issues/source
    # links filled in). Fall back to a cached GitHub-repo scan (matches
    # a snapcraft.yaml "name:" to this snap) if it's already warm; if
    # it's cold, kick off a background scan so the next page load has it
    # without blocking this request on ~150 GitHub API calls.
    if not snap_data["packaging_repo"] and not snap_data["packaging_repo_suggested"]:
        uc = get_user_config(user_id)
        token = uc.bot_github_token or uc.github_token
        if token:
            cached_map = get_cached_packaging_repo_map(token)
            if cached_map is not None:
                discovered = cached_map.get(snap_data["name"])
                if discovered:
                    snap_data["packaging_repo_suggested"] = discovered
            else:
                def _warm_cache(tok: str = token) -> None:
                    build_packaging_repo_map(tok)

                background_tasks.add_task(_warm_cache)

    return templates.TemplateResponse(
        request,
        "snap_detail.html",
        {
            "snap": snap_data,
            "arch_map": arch_map,
            "cm_rows": cm_data,
            "issues": issues_data,
            "test_runs": test_runs_data,
            "last_run": _get_last_run(user_id),
            "channels": ["stable", "candidate", "beta", "edge"],
            "current_user": user,
        },
    )


_executor = ThreadPoolExecutor(max_workers=2)


@router.post("/snap/{name}/refresh")
async def snap_refresh(
    name: str,
    request: Request,
    background_tasks: BackgroundTasks,
) -> RedirectResponse:
    """Trigger a collection run for a single snap then redirect to its detail page."""
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]

    def _bg() -> None:
        from snap_dashboard.collector import collect_one
        uc = get_user_config(user_id)
        config = uc.to_config()
        collect_one(config, name, user_id=user_id)

    background_tasks.add_task(_bg)
    return RedirectResponse(url=f"/snap/{name}", status_code=303)


@router.post("/snap/{name}/check-updates")
async def snap_check_updates(name: str, request: Request) -> RedirectResponse:
    """Manually scan this one snap's packaging repo for a newer upstream
    version and spawn a version-bump PR if one is found — the same thing
    ReleaseScannerAgent does for the whole portfolio on its schedule, just
    scoped to a single snap so users don't have to wait for the next pass.
    """
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]
    with get_session() as session:
        snap = session.query(Snap).filter_by(name=name, user_id=user_id).first()
        if not snap:
            return RedirectResponse(url="/", status_code=303)
        if not snap.packaging_repo:
            return RedirectResponse(url=f"/snap/{name}?error=no_packaging_repo", status_code=303)
        snap_id = snap.id

    from snap_dashboard.agents.release_scanner import ReleaseScannerAgent
    from snap_dashboard.agents.runner import get_runner

    get_runner().submit(ReleaseScannerAgent(user_id=user_id, snap_id=snap_id))
    return RedirectResponse(url=f"/snap/{name}?notice=scan_started", status_code=303)


@router.post("/snap/{name}/trigger-test")
async def snap_trigger_test(
    name: str,
    request: Request,
    from_channel: str = Form(default="candidate"),
    architecture: str = Form(default="amd64"),
    version: str = Form(default=""),
    revision: str = Form(default="0"),
) -> RedirectResponse:
    """Queue a YARF test run for this snap directly from its detail page.

    Thin wrapper around the same remote-runner dispatch used by the
    Testing page (see testing.trigger_test) — kept here so the redirect
    lands back on /snap/{name} instead of /testing.
    """
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    from snap_dashboard.testing.orchestrator import trigger_remote_run

    user_id = user["id"]
    rev: int | None = int(revision) if revision.isdigit() and int(revision) > 0 else None

    ok, err, _db_run_id = trigger_remote_run(
        name, from_channel, version, rev,
        architecture=architecture,
        triggered_by="manual",
        user_id=user_id,
    )
    if not ok:
        logger.error("Failed to trigger test for %s: %s", name, err)
        return RedirectResponse(url=f"/snap/{name}?error=trigger_failed", status_code=303)

    return RedirectResponse(url=f"/snap/{name}?notice=test_queued", status_code=303)


@router.post("/snap/{name}/edit")
async def snap_edit(
    request: Request,
    name: str,
    packaging_repo: str = Form(default=""),
    upstream_repo: str = Form(default=""),
    notes: str = Form(default=""),
    is_console_app: str = Form(default=""),
) -> RedirectResponse:
    """Update snap metadata."""
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]

    with get_session() as session:
        snap = session.query(Snap).filter_by(name=name, user_id=user_id).first()
        if not snap:
            return RedirectResponse(url="/", status_code=303)
        snap.packaging_repo = packaging_repo.strip() or None
        snap.upstream_repo = upstream_repo.strip() or None
        snap.notes = notes.strip() or None
        snap.is_console_app = is_console_app == "true"

    return RedirectResponse(url=f"/snap/{name}", status_code=303)
