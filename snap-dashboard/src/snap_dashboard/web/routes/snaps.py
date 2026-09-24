"""Snap management routes — add, view, edit."""

from __future__ import annotations

import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from snap_dashboard.auth import get_current_user, get_user_config
from snap_dashboard.db.models import ChannelMap, CollectionRun, Issue, Snap
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
async def snap_search(
    request: Request,
    snap_name: str = Form(...),
) -> HTMLResponse:
    """Search Snap Store for a snap and return an HTML fragment for HTMX swap."""
    snap_name = snap_name.strip().lower()
    info = get_snap_info(snap_name)

    if not info:
        html = (
            '<div id="search-results" class="search-result not-found">'
            f'<p class="error-msg">Snap <strong>{snap_name}</strong> not found in the store.</p>'
            "</div>"
        )
        return HTMLResponse(content=html)

    snap_section = info.get("snap", {}) or {}
    publisher_info = snap_section.get("publisher", {}) or {}
    publisher = publisher_info.get("username", "") if isinstance(publisher_info, dict) else ""

    repos = extract_repo_urls(info)
    packaging_repo = repos.get("packaging_repo") or ""
    upstream_repo = repos.get("upstream_repo") or ""

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
async def snap_detail(request: Request, name: str, background_tasks: BackgroundTasks) -> HTMLResponse:
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
        with get_session() as session:
            collect_one(session, config, name, user_id=user_id)

    background_tasks.add_task(_bg)
    return RedirectResponse(url=f"/snap/{name}", status_code=303)


@router.post("/snap/{name}/edit")
async def snap_edit(
    request: Request,
    name: str,
    packaging_repo: str = Form(default=""),
    upstream_repo: str = Form(default=""),
    notes: str = Form(default=""),
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

    return RedirectResponse(url=f"/snap/{name}", status_code=303)
