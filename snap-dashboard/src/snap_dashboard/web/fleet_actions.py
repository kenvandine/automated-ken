"""Fleet-wide snap actions shared by the Snaps page and legacy Settings URLs.

Every action is exposed through a route that answers JSON for ``fetch()``
callers (``X-Requested-With`` header) — ``{"ok": bool, "message": str}``
plus the historic keys (``started`` / ``removed`` / ``error``) — and falls
back to a redirect for plain form posts.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from snap_dashboard.auth import get_current_user, get_user_config
from snap_dashboard.db.models import (
    PromotionDismissal,
    Runner,
    Snap,
    StableScreenshotBaseline,
    TestRun,
    TestRunScreenshot,
)


def remove_snap(session, user_id: int, snap_name: str) -> bool:
    """Stop tracking *snap_name* and delete everything keyed to it.

    TestRun/StableScreenshotBaseline/PromotionDismissal key off
    ``snap_name`` (a string) rather than ``snap_id``, so they aren't
    covered by Snap's ORM cascades and would otherwise survive the delete
    — leaving stale cards (e.g. "Pending Promotion") for a snap that's no
    longer tracked. Returns False when the snap wasn't found.
    """
    snap = session.query(Snap).filter_by(name=snap_name, user_id=user_id).first()
    if snap is None:
        return False
    run_ids = [
        r.id
        for r in session.query(TestRun.id).filter_by(snap_name=snap_name, user_id=user_id).all()
    ]
    if run_ids:
        session.query(TestRunScreenshot).filter(
            TestRunScreenshot.test_run_id.in_(run_ids)
        ).delete(synchronize_session=False)
        # Runners can point at one of these runs as their "currently
        # executing" job — clear that back-reference rather than leaving
        # it dangling.
        session.query(Runner).filter(
            Runner.user_id == user_id,
            Runner.current_test_run_id.in_(run_ids),
        ).update({"current_test_run_id": None}, synchronize_session=False)
        session.query(TestRun).filter(TestRun.id.in_(run_ids)).delete(synchronize_session=False)
    session.query(StableScreenshotBaseline).filter_by(
        snap_name=snap_name, user_id=user_id
    ).delete(synchronize_session=False)
    session.query(PromotionDismissal).filter_by(
        snap_name=snap_name, user_id=user_id
    ).delete(synchronize_session=False)
    session.delete(snap)
    return True


# action key -> (config precondition, error code, error message, success message)
FLEET_ACTIONS = {
    "rebuild-all": (
        lambda uc: bool(getattr(uc, "github_token", "") or ""),
        "rebuild_needs_github_token",
        "Add a GitHub token in Settings before rebuilding.",
        "Rebuild started for every snap with a build workflow — follow progress under Agents.",
    ),
    "sync-credentials": (
        lambda uc: bool(getattr(uc, "snapcraft_macaroon", "") or ""),
        "no_snapcraft_credential",
        "Add a Snap Store credential in Settings first.",
        "Syncing the Store credential to every packaging repo — follow progress under Agents.",
    ),
    "normalize-fleet": (
        lambda uc: bool(getattr(uc, "fleet_normalization_enabled", False)),
        "fleet_normalization_disabled",
        "Enable fleet normalization in Settings first.",
        "Fleet normalization started — follow progress under Agents.",
    ),
}


def _make_agent(action: str, user_id: int):
    from snap_dashboard.agents.runner import get_runner

    if action == "rebuild-all":
        from snap_dashboard.agents.stale_build_scanner import RebuildAllSnapsAgent

        agent = RebuildAllSnapsAgent(user_id=user_id)
    elif action == "sync-credentials":
        from snap_dashboard.agents.snapcraft_credential_sync import SnapcraftCredentialSyncAgent

        agent = SnapcraftCredentialSyncAgent(user_id=user_id)
    else:
        from snap_dashboard.agents.repo_normalizer import RepoNormalizerAgent

        agent = RepoNormalizerAgent(user_id=user_id)
    get_runner().submit(agent)


def run_fleet_action(request: Request, action: str, error_redirect: str = "/snaps"):
    """Validate and submit one of FLEET_ACTIONS, answering JSON or redirect."""
    is_fetch = bool(request.headers.get("X-Requested-With"))
    user = get_current_user(request)
    if user is None:
        if is_fetch:
            return JSONResponse({"ok": False, "error": "not authenticated", "message": "Sign in again."}, status_code=401)
        return RedirectResponse(url="/auth/login", status_code=302)

    spec = FLEET_ACTIONS.get(action)
    if spec is None:
        if is_fetch:
            return JSONResponse({"ok": False, "error": "unknown_action", "message": "Unknown action."}, status_code=404)
        return RedirectResponse(url=error_redirect, status_code=303)
    check, err_code, err_msg, ok_msg = spec

    uc = get_user_config(user["id"])
    if not uc or not check(uc):
        if is_fetch:
            return JSONResponse({"ok": False, "error": err_code, "message": err_msg}, status_code=400)
        sep = "&" if "?" in error_redirect else "?"
        return RedirectResponse(url=f"{error_redirect}{sep}error={err_code}", status_code=303)

    _make_agent(action, user["id"])
    if is_fetch:
        return JSONResponse({"ok": True, "started": True, "message": ok_msg})
    return RedirectResponse(url="/agents", status_code=303)
