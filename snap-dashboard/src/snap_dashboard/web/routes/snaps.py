"""Snap management routes — add, view, edit."""

from __future__ import annotations

import logging
from datetime import timezone
from pathlib import Path

from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from snap_dashboard.config import get_config
from snap_dashboard.db.models import ChannelMap, Issue, Snap
from snap_dashboard.db.session import get_session
from snap_dashboard.store.client import extract_repo_urls, get_snap_info

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _get_last_run():
    from snap_dashboard.db.models import CollectionRun
    with get_session() as session:
        run = (
            session.query(CollectionRun)
            .filter(CollectionRun.status == "success")
            .order_by(CollectionRun.finished_at.desc())
            .first()
        )
        return run.finished_at if run and run.finished_at else None


@router.get("/snaps/add", response_class=HTMLResponse)
async def snap_add_get(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "snap_add.html",
        {
            "request": request,
            "last_run": _get_last_run(),
            "search_result": None,
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
    snap_name = snap_name.strip().lower()
    config = get_config()

    with get_session() as session:
        existing = session.query(Snap).filter_by(name=snap_name).first()
        if existing:
            return RedirectResponse(url=f"/snap/{snap_name}", status_code=303)

        snap = Snap(
            name=snap_name,
            publisher=publisher or config.publisher or "",
            manually_added=True,
            packaging_repo=packaging_repo.strip() or None,
            upstream_repo=upstream_repo.strip() or None,
            notes=notes.strip() or None,
        )
        session.add(snap)

    return RedirectResponse(url=f"/snap/{snap_name}", status_code=303)


@router.get("/snap/{name}", response_class=HTMLResponse)
async def snap_detail(request: Request, name: str) -> HTMLResponse:
    with get_session() as session:
        snap = session.query(Snap).filter_by(name=name).first()
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
        }

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

    return templates.TemplateResponse(
        "snap_detail.html",
        {
            "request": request,
            "snap": snap_data,
            "arch_map": arch_map,
            "cm_rows": cm_data,
            "issues": issues_data,
            "last_run": _get_last_run(),
            "channels": ["stable", "candidate", "beta", "edge"],
        },
    )


_executor = ThreadPoolExecutor(max_workers=2)


@router.post("/snap/{name}/refresh")
async def snap_refresh(
    name: str,
    background_tasks: BackgroundTasks,
) -> RedirectResponse:
    """Trigger a collection run for a single snap then redirect to its detail page."""
    from snap_dashboard.collector import collect_one

    def _bg() -> None:
        config = get_config()
        with get_session() as session:
            collect_one(session, config, name)

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
    with get_session() as session:
        snap = session.query(Snap).filter_by(name=name).first()
        if not snap:
            return RedirectResponse(url="/", status_code=303)
        snap.packaging_repo = packaging_repo.strip() or None
        snap.upstream_repo = upstream_repo.strip() or None
        snap.notes = notes.strip() or None

    return RedirectResponse(url=f"/snap/{name}", status_code=303)
