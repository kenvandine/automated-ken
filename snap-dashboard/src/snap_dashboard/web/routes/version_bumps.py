"""Version bump PR routes — review, merge, reject."""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from snap_dashboard.auth import get_current_user, get_user_config
from snap_dashboard.db.models import ScreenshotComparison, VersionBumpPR
from snap_dashboard.db.session import get_session
from snap_dashboard.github.utils import parse_owner_repo
from snap_dashboard.testing.release_set import candidate_release_set, member_state
from snap_dashboard.web.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()

_GH_API = "https://api.github.com"

# Statuses in priority display order
_STATUS_GROUPS = [
    ("stable_promoted", "Stable Promoted"),
    ("stable_promoted_partial", "Partially Promoted (Override)"),
    ("candidate_testing", "Merged — Testing Candidate for Stable"),
    ("awaiting_release", "Merged — Waiting for Candidate Release"),
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
    ("dispatched", "Coding Agent Working…"),
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
        bump_list = [_serialise_bump(session, b) for b in bumps]

    # Group by status
    groups: list[dict] = []
    for status_key, label in _STATUS_GROUPS:
        items = [b for b in bump_list if b["status"] == status_key]
        if items:
            groups.append({"status": status_key, "label": label, "bumps": items})
    # Statuses no longer produced (e.g. from older versions) still show up.
    known = {key for key, _ in _STATUS_GROUPS}
    for status_key in sorted({b["status"] for b in bump_list} - known):
        items = [b for b in bump_list if b["status"] == status_key]
        groups.append({"status": status_key, "label": status_key.replace("_", " ").title(), "bumps": items})

    # Count summary for nav badge
    actionable = sum(
        1
        for b in bump_list
        if b["status"] in ("agent_approved", "needs_review")
    )

    return templates.TemplateResponse(
        request,
        "version_bumps.html",
        {
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

        bump_data = _serialise_bump(session, bump)

        # One comparison per architecture (see agents/screenshot_reviewer.py)
        # — keep the latest per test_run_id and order to match bump_data's
        # architecture list so the two line up in the template.
        run_ids = [a["test_run_id"] for a in bump_data["architectures"] if a["test_run_id"]]
        latest_by_run: dict[int, ScreenshotComparison] = {}
        if run_ids:
            for row in (
                session.query(ScreenshotComparison)
                .filter(ScreenshotComparison.test_run_id.in_(run_ids))
                .order_by(ScreenshotComparison.id.asc())
                .all()
            ):
                latest_by_run[row.test_run_id] = row
        comparisons = [
            {
                "architecture": a["architecture"],
                **_serialise_comp(latest_by_run[a["test_run_id"]]),
            }
            for a in bump_data["architectures"]
            if a["test_run_id"] in latest_by_run
        ]

        # After merge, the thing that gets promoted is the new version's
        # candidate release set (see agents/pr_monitor.py) — not the
        # pre-merge edge runs above.
        candidate_set = []
        if bump.merged_at and bump_data["snap_name"] and bump_data["new_version"]:
            for member in candidate_release_set(
                session, user["id"], bump_data["snap_name"], bump_data["new_version"]
            ):
                candidate_set.append(
                    {
                        "architecture": member["architecture"],
                        "run_id": member["run"].id if member["run"] else None,
                        "revision": member["revision"],
                        "state": member_state(member),
                    }
                )

    return templates.TemplateResponse(
        request,
        "version_bump_detail.html",
        {
            "current_user": user,
            "bump": bump_data,
            "comparisons": comparisons,
            "candidate_set": candidate_set,
            "last_run": None,
        },
    )


@router.post("/version-bumps/{bump_id}/merge")
def merge_bump(bump_id: int, request: Request) -> RedirectResponse:
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
            from snap_dashboard.agents.pr_monitor import mark_bump_merged

            mark_bump_merged(bump_id)
    except Exception as exc:
        logger.warning("merge PR %s failed: %s", bump_id, exc)

    return RedirectResponse(url="/version-bumps", status_code=303)


@router.post("/version-bumps/{bump_id}/reject")
def reject_bump(bump_id: int, request: Request) -> RedirectResponse:
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

    with get_session() as session:
        bump = session.query(VersionBumpPR).filter_by(id=bump_id, user_id=user["id"]).first()
        if not bump:
            return RedirectResponse(url="/version-bumps", status_code=302)
        snap_id = bump.snap_id
        snap_name = bump.snap.name if bump.snap else ""
        new_version = bump.new_version or ""
        merged = bump.merged_at is not None

    if snap_name and merged:
        # After merge, re-test what would actually ship: the candidate set.
        from snap_dashboard.testing.release_set import queue_candidate_tests

        if queue_candidate_tests(user["id"], snap_name, new_version, force=True):
            with get_session() as session:
                bump = session.query(VersionBumpPR).get(bump_id)
                if bump:
                    bump.status = "candidate_testing"
    elif snap_name:
        # Tests run on registered remote runners, not GitHub Actions — see
        # snap_dashboard.db.models.Runner. Fans out one run per testable
        # architecture, same as the automated pipeline (agents/pr_monitor.py)
        # so a manual re-run still tests/promotes every arch together.
        from snap_dashboard.testing.orchestrator import queue_yarf_tests_for_bump
        run_ids, errors = queue_yarf_tests_for_bump(
            snap_id=snap_id,
            snap_name=snap_name,
            version=new_version,
            user_id=user["id"],
            version_bump_pr_id=bump_id,
            triggered_by="manual",
        )
        if errors:
            logger.warning("re_run_yarf: partial/full failure for bump %s: %s", bump_id, "; ".join(errors))
        if run_ids:
            with get_session() as session:
                bump = session.query(VersionBumpPR).get(bump_id)
                if bump:
                    bump.status = "yarf_running"
                    bump.test_run_id = run_ids[0]

    return RedirectResponse(url=f"/version-bumps/{bump_id}", status_code=303)


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _serialise_bump(session, b: VersionBumpPR) -> dict:
    # One row per architecture (see agents/pr_monitor.py:_trigger_yarf /
    # orchestrator.queue_yarf_tests_for_bump) so the whole release set —
    # amd64, arm64, ... — displays and gets acted on together instead of
    # as disconnected individual runs.
    from snap_dashboard.testing.orchestrator import latest_bump_runs

    sibling_runs = latest_bump_runs(session, b.id, b.test_run_id)
    architectures = [
        {
            "architecture": r.architecture or "amd64",
            "test_run_id": r.id,
            "status": r.status,
            "revision": r.revision,
            "promoted": r.promoted,
        }
        for r in sibling_runs
    ]

    return {
        "id": b.id,
        "snap_id": b.snap_id,
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
        "architectures": architectures,
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
