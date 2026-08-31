"""Version bump PR routes — review, merge, reject."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from snap_dashboard.auth import get_current_user, get_user_config
from snap_dashboard.db.models import ScreenshotComparison, VersionBumpPR
from snap_dashboard.db.session import get_session
from snap_dashboard.github.utils import parse_owner_repo

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

_GH_API = "https://api.github.com"

# Statuses in priority display order
_STATUS_GROUPS = [
    ("stable_promoted", "Stable Promoted"),
    ("promoting", "Promoting to Stable"),
    ("promotion_failed", "Promotion Failed"),
    ("agent_approved", "Agent Approved"),
    ("needs_review", "Needs Your Review"),
    ("agent_rejected", "Agent Rejected"),
    ("yarf_passed", "YARF Passed — Awaiting Review"),
    ("yarf_failed", "YARF Failed"),
    ("yarf_running", "YARF Running"),
    ("ci_passed", "CI Passed"),
    ("ci_failed", "CI Failed"),
    ("ci_pending", "CI Pending"),
    ("open", "Open"),
    ("merged", "Merged"),
    ("closed", "Closed"),
]


@router.get("/version-bumps", response_class=HTMLResponse)
async def version_bumps_page(request: Request) -> HTMLResponse:
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]

    with get_session() as session:
        bumps = (
            session.query(VersionBumpPR)
            .filter_by(user_id=user_id)
            .order_by(VersionBumpPR.created_at.desc())
            .all()
        )
        bump_list = [_serialise_bump(b) for b in bumps]

    # Group by status
    groups: list[dict] = []
    for status_key, label in _STATUS_GROUPS:
        items = [b for b in bump_list if b["status"] == status_key]
        if items:
            groups.append({"status": status_key, "label": label, "items": items})

    # Count summary for nav badge
    actionable = sum(
        1
        for b in bump_list
        if b["status"] in ("agent_approved", "needs_review", "promotion_failed")
    )

    return templates.TemplateResponse(
        "version_bumps.html",
        {
            "request": request,
            "current_user": user,
            "groups": groups,
            "actionable_count": actionable,
            "last_run": None,
        },
    )


@router.get("/version-bumps/{bump_id}", response_class=HTMLResponse)
async def version_bump_detail(bump_id: int, request: Request) -> HTMLResponse:
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    with get_session() as session:
        bump = session.query(VersionBumpPR).filter_by(id=bump_id, user_id=user["id"]).first()
        if not bump:
            return RedirectResponse(url="/version-bumps", status_code=302)

        bump_data = _serialise_bump(bump)

        comp = (
            session.query(ScreenshotComparison)
            .filter_by(version_bump_pr_id=bump_id)
            .order_by(ScreenshotComparison.analyzed_at.desc())
            .first()
        )
        comp_data = _serialise_comp(comp) if comp else None

    return templates.TemplateResponse(
        "version_bump_detail.html",
        {
            "request": request,
            "current_user": user,
            "bump": bump_data,
            "comparison": comp_data,
            "last_run": None,
        },
    )


@router.post("/version-bumps/{bump_id}/merge")
async def merge_bump(bump_id: int, request: Request) -> RedirectResponse:
    """Merge the bot PR using the user's primary GitHub token."""
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    uc = get_user_config(user["id"])
    token = uc.github_token or ""

    with get_session() as session:
        bump = session.query(VersionBumpPR).filter_by(id=bump_id, user_id=user["id"]).first()
        if not bump or not bump.packaging_repo or not bump.bot_pr_number:
            return RedirectResponse(url="/version-bumps", status_code=302)
        owner_repo = parse_owner_repo(bump.packaging_repo)
        pr_number = bump.bot_pr_number

    if not owner_repo:
        return RedirectResponse(url="/version-bumps", status_code=302)
    owner, repo = owner_repo
    url = f"{_GH_API}/repos/{owner}/{repo}/pulls/{pr_number}/merge"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
    }
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.put(url, json={"merge_method": "squash"}, headers=headers)
        if resp.status_code in (200, 201):
            with get_session() as session:
                bump = session.query(VersionBumpPR).get(bump_id)
                if bump:
                    from datetime import datetime, timezone
                    bump.status = "merged"
                    bump.merged_at = datetime.now(timezone.utc)
    except Exception as exc:
        logger.warning("merge PR %s failed: %s", bump_id, exc)

    return RedirectResponse(url="/version-bumps", status_code=303)


@router.post("/version-bumps/{bump_id}/reject")
async def reject_bump(bump_id: int, request: Request) -> RedirectResponse:
    """Close the bot PR and mark as rejected."""
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    uc = get_user_config(user["id"])
    token = uc.github_token or ""

    with get_session() as session:
        bump = session.query(VersionBumpPR).filter_by(id=bump_id, user_id=user["id"]).first()
        if not bump:
            return RedirectResponse(url="/version-bumps", status_code=302)
        owner_repo = parse_owner_repo(bump.packaging_repo or "")
        pr_number = bump.bot_pr_number

    if owner_repo and pr_number and token:
        owner, repo = owner_repo
        url = f"{_GH_API}/repos/{owner}/{repo}/pulls/{pr_number}"
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
        }
        try:
            with httpx.Client(timeout=15) as client:
                client.patch(url, json={"state": "closed"}, headers=headers)
        except Exception as exc:
            logger.warning("close PR %s failed: %s", bump_id, exc)

    with get_session() as session:
        bump = session.query(VersionBumpPR).get(bump_id)
        if bump:
            bump.status = "closed"

    return RedirectResponse(url="/version-bumps", status_code=303)


@router.post("/version-bumps/{bump_id}/request-review")
async def request_review(bump_id: int, request: Request) -> RedirectResponse:
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    with get_session() as session:
        bump = session.query(VersionBumpPR).filter_by(id=bump_id, user_id=user["id"]).first()
        if bump:
            bump.status = "needs_review"

    return RedirectResponse(url=f"/version-bumps/{bump_id}", status_code=303)


@router.post("/version-bumps/{bump_id}/re-run-yarf")
async def re_run_yarf(bump_id: int, request: Request) -> RedirectResponse:
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    uc = get_user_config(user["id"])

    with get_session() as session:
        bump = session.query(VersionBumpPR).filter_by(id=bump_id, user_id=user["id"]).first()
        if not bump:
            return RedirectResponse(url="/version-bumps", status_code=302)
        snap_name = bump.snap.name if bump.snap else ""
        new_version = bump.new_version or ""

    if snap_name and uc.testing_repo and uc.github_token:
        from snap_dashboard.testing.orchestrator import trigger_workflow
        ok, err, run_id = trigger_workflow(
            snap_name=snap_name,
            from_channel="edge",
            version=new_version,
            revision=None,
            triggered_by="manual",
            testing_repo=uc.testing_repo,
            github_token=uc.github_token,
            user_id=user["id"],
        )
        if ok and run_id:
            with get_session() as session:
                bump = session.query(VersionBumpPR).get(bump_id)
                if bump:
                    bump.status = "yarf_running"
                    bump.test_run_id = run_id

    return RedirectResponse(url=f"/version-bumps/{bump_id}", status_code=303)


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _serialise_bump(b: VersionBumpPR) -> dict:
    return {
        "id": b.id,
        "snap_name": b.snap.name if b.snap else "",
        "part_name": b.upstream_release.part_name if b.upstream_release else "",
        "old_version": b.old_version or "",
        "new_version": b.new_version or "",
        "status": b.status,
        "bot_pr_url": b.bot_pr_url or "",
        "bot_pr_number": b.bot_pr_number,
        "packaging_repo": b.packaging_repo or "",
        "agent_decision": b.agent_decision or "",
        "agent_confidence": b.agent_confidence,
        "agent_reasoning": b.agent_reasoning or "",
        "created_at": b.created_at,
        "merged_at": b.merged_at,
    }


def _serialise_comp(c: ScreenshotComparison) -> dict:
    return {
        "id": c.id,
        "baseline_url": c.baseline_url or "",
        "baseline_image_b64": c.baseline_image_b64 or "",
        "new_url": c.new_url or "",
        "new_image_b64": c.new_image_b64 or "",
        "decision": c.decision or "",
        "confidence": c.confidence,
        "reasoning": c.reasoning or "",
        "analyzed_at": c.analyzed_at,
    }
