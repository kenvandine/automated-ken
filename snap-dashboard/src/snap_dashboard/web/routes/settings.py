"""Settings page routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from snap_dashboard.auth import get_current_user, get_user_config
from snap_dashboard.db.models import (
    CollectionRun,
    PromotionDismissal,
    Runner,
    Snap,
    StableScreenshotBaseline,
    TestRun,
    TestRunScreenshot,
    UserConfig,
)
from snap_dashboard.db.session import get_session
from snap_dashboard.web.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_last_run(user_id: int):
    with get_session() as session:
        run = (
            session.query(CollectionRun)
            .filter_by(user_id=user_id, status="success")
            .order_by(CollectionRun.finished_at.desc())
            .first()
        )
        return run.finished_at if run and run.finished_at else None


@router.get("/settings", response_class=HTMLResponse)
async def settings_get(request: Request) -> HTMLResponse:
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]
    uc = get_user_config(user_id)

    with get_session() as session:
        snaps = (
            session.query(Snap)
            .filter_by(user_id=user_id)
            .order_by(Snap.name)
            .all()
        )
        snap_list = [
            {
                "name": s.name,
                "publisher": s.publisher,
                "manually_added": s.manually_added,
                "packaging_repo": s.packaging_repo,
                "upstream_repo": s.upstream_repo,
            }
            for s in snaps
        ]

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "config": uc,
            "snaps": snap_list,
            "last_run": _get_last_run(user_id),
            "intervals": [1, 6, 12, 24],
            "current_user": user,
        },
    )


@router.post("/settings")
async def settings_post(
    request: Request,
    section: str = Form(default=""),
    publisher: str = Form(default=""),
    github_token: str = Form(default=""),
    snapcraft_macaroon: str = Form(default=""),
    interval: int = Form(default=6),
    auto_test: str = Form(default=""),
    runner_job_timeout_minutes: int = Form(default=10),
    lemonade_server_url: str = Form(default=""),
    lemonade_model: str = Form(default=""),
    lemonade_backend: str = Form(default="embedded"),
    lemonade_api_key: str = Form(default=""),
    bot_github_token: str = Form(default=""),
    bot_github_login: str = Form(default=""),
    agent_interval_hours: int = Form(default=4),
    auto_merge: str = Form(default=""),
    auto_promote: str = Form(default=""),
    auto_promote_confidence: float = Form(default=0.85),
    auto_rebuild_stale: str = Form(default=""),
    stale_build_days: int = Form(default=30),
    auto_fix_ci_failures: str = Form(default=""),
    auto_maintain_upstream: str = Form(default=""),
    fleet_normalization_enabled: str = Form(default=""),
    coding_task_backend: str = Form(default="copilot_cloud_agent"),
    external_coding_api_key: str = Form(default=""),
    external_coding_api_base_url: str = Form(default=""),
    external_coding_api_model: str = Form(default=""),
) -> RedirectResponse:
    """Save per-user settings to UserConfig in the database.

    The settings page is split into several independent ``<form>`` cards
    (Publisher, GitHub Token, Snapcraft Credential, Testing, Agents & AI),
    each with its own Save button, but they all post here. Each form only
    includes its own fields in the submitted body -- notably, unchecked
    HTML checkboxes are omitted entirely, indistinguishable from a
    checkbox that simply isn't part of the submitted form. Without knowing
    which card was submitted, saving e.g. just the "Testing" card would
    silently reset every checkbox/field belonging to the *other* cards
    (like "Enable fleet normalization campaign") back to its Form default.
    Each form therefore carries a hidden ``section`` field so we only
    touch the fields that actually belong to the submitted card.
    """
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]
    # Unknown/missing section (e.g. a stale cached page) -- fall back to
    # updating every field present, matching the historical behavior of a
    # single monolithic form, rather than silently doing nothing.
    all_sections = {"publisher", "github_token", "snapcraft_macaroon", "testing", "agents_ai"}
    sections_to_apply = {section} if section in all_sections else all_sections

    _auto_test = auto_test in ("1", "true", "on", "yes")
    _auto_merge = auto_merge in ("1", "true", "on", "yes")
    _auto_promote = auto_promote in ("1", "true", "on", "yes")
    _auto_rebuild_stale = auto_rebuild_stale in ("1", "true", "on", "yes")
    _auto_fix_ci_failures = auto_fix_ci_failures in ("1", "true", "on", "yes")
    _auto_maintain_upstream = auto_maintain_upstream in ("1", "true", "on", "yes")
    _fleet_normalization_enabled = fleet_normalization_enabled in ("1", "true", "on", "yes")

    with get_session() as session:
        uc = session.query(UserConfig).filter_by(user_id=user_id).first()
        if uc is None:
            uc = UserConfig(user_id=user_id)
            session.add(uc)

        if "publisher" in sections_to_apply:
            if publisher.strip():
                uc.publisher = publisher.strip()
            uc.collect_interval_hours = interval

        if "github_token" in sections_to_apply and github_token.strip():
            uc.github_token = github_token.strip()

        if "snapcraft_macaroon" in sections_to_apply and snapcraft_macaroon.strip():
            uc.snapcraft_macaroon = snapcraft_macaroon.strip()

        if "testing" in sections_to_apply:
            uc.auto_test = _auto_test
            uc.runner_job_timeout_minutes = max(1, min(240, runner_job_timeout_minutes))

        if "agents_ai" in sections_to_apply:
            if lemonade_server_url.strip():
                uc.lemonade_server_url = lemonade_server_url.strip()
            if lemonade_model.strip():
                uc.lemonade_model = lemonade_model.strip()
            uc.lemonade_backend = lemonade_backend.strip() if lemonade_backend.strip() in ("embedded", "system") else "embedded"
            if lemonade_api_key.strip():
                uc.lemonade_api_key = lemonade_api_key.strip()
            if bot_github_token.strip():
                uc.bot_github_token = bot_github_token.strip()
            if bot_github_login.strip():
                uc.bot_github_login = bot_github_login.strip()
            uc.agent_interval_hours = agent_interval_hours
            uc.auto_merge = _auto_merge
            uc.auto_promote = _auto_promote
            uc.auto_promote_confidence = max(0.0, min(1.0, auto_promote_confidence))
            uc.auto_rebuild_stale = _auto_rebuild_stale
            uc.stale_build_days = max(1, stale_build_days)
            # Copilot cloud agent delegation toggles (all default off / opt-in)
            uc.auto_fix_ci_failures = _auto_fix_ci_failures
            uc.auto_maintain_upstream = _auto_maintain_upstream
            uc.fleet_normalization_enabled = _fleet_normalization_enabled
            # Pluggable coding-task backend — see agents/coding_backend.py
            if coding_task_backend.strip():
                uc.coding_task_backend = coding_task_backend.strip()
            if external_coding_api_key.strip():
                uc.external_coding_api_key = external_coding_api_key.strip()
            if external_coding_api_base_url.strip():
                uc.external_coding_api_base_url = external_coding_api_base_url.strip()
            if external_coding_api_model.strip():
                uc.external_coding_api_model = external_coding_api_model.strip()

    # Reschedule agents with the new settings
    try:
        from snap_dashboard.agents.runner import get_runner
        from snap_dashboard.agents.scheduling import schedule_user_agents
        schedule_user_agents(get_runner(), user_id, get_user_config(user_id))
    except Exception as exc:
        logger.warning("failed to reschedule agents: %s", exc)

    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/remove/{snap_name}")
async def settings_remove_snap(snap_name: str, request: Request):
    """Remove a snap from tracking.

    Called via ``fetch()`` from the Settings page's Remove button, which
    removes the row in place instead of a full page reload — respond with
    a small JSON body rather than a redirect so that path doesn't need a
    second round-trip to fetch/parse the whole page. Non-JS form
    submissions (no ``X-Requested-With`` header) still get the old
    redirect-to-/settings behavior as a fallback.
    """
    user = get_current_user(request)
    if user is None:
        if request.headers.get("X-Requested-With"):
            return JSONResponse({"error": "not authenticated"}, status_code=401)
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]

    with get_session() as session:
        snap = session.query(Snap).filter_by(name=snap_name, user_id=user_id).first()
        if snap:
            # TestRun/StableScreenshotBaseline key off `snap_name` (a
            # string) rather than `snap_id`, so they aren't covered by
            # Snap's ORM cascade relationships (see models.py) and would
            # otherwise survive this delete — leaving stale cards (e.g.
            # "Pending Promotion" on /testing) for a snap that's no
            # longer tracked. Clean those up explicitly so removing a
            # snap here removes it everywhere.
            runs = (
                session.query(TestRun)
                .filter_by(snap_name=snap_name, user_id=user_id)
                .all()
            )
            run_ids = [r.id for r in runs]
            if run_ids:
                session.query(TestRunScreenshot).filter(
                    TestRunScreenshot.test_run_id.in_(run_ids)
                ).delete(synchronize_session=False)
                # Runners can point at one of these runs as their
                # "currently executing" job — clear that back-reference
                # rather than leaving it dangling.
                session.query(Runner).filter(
                    Runner.user_id == user_id,
                    Runner.current_test_run_id.in_(run_ids),
                ).update({"current_test_run_id": None}, synchronize_session=False)
                session.query(TestRun).filter(
                    TestRun.id.in_(run_ids)
                ).delete(synchronize_session=False)
            session.query(StableScreenshotBaseline).filter_by(
                snap_name=snap_name, user_id=user_id
            ).delete(synchronize_session=False)
            session.query(PromotionDismissal).filter_by(
                snap_name=snap_name, user_id=user_id
            ).delete(synchronize_session=False)
            session.delete(snap)

    if request.headers.get("X-Requested-With"):
        return JSONResponse({"removed": snap_name})
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/run-fleet-normalization")
async def settings_run_fleet_normalization(request: Request):
    """Manually trigger the one-time fleet-normalization campaign.

    Unlike the periodic agents, this is a deliberate one-off run — a user
    clicks this after enabling ``fleet_normalization_enabled`` to kick off
    the pass across all packaging repos.

    Called via ``fetch()`` from the Settings page so it doesn't reload the
    whole page — respond with JSON when ``X-Requested-With`` is present.
    Non-JS form submissions still get the old redirect fallback.
    """
    is_fetch = bool(request.headers.get("X-Requested-With"))

    user = get_current_user(request)
    if user is None:
        if is_fetch:
            return JSONResponse({"error": "not authenticated"}, status_code=401)
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]
    uc = get_user_config(user_id)
    if not uc or not getattr(uc, "fleet_normalization_enabled", False):
        if is_fetch:
            return JSONResponse({"error": "fleet_normalization_disabled"}, status_code=400)
        return RedirectResponse(url="/settings?error=fleet_normalization_disabled", status_code=303)

    from snap_dashboard.agents.repo_normalizer import RepoNormalizerAgent
    from snap_dashboard.agents.runner import get_runner

    get_runner().submit(RepoNormalizerAgent(user_id=user_id))
    if is_fetch:
        return JSONResponse({"started": True})
    return RedirectResponse(url="/agents", status_code=303)


@router.post("/settings/rebuild-all-snaps")
async def settings_rebuild_all_snaps(request: Request):
    """Manually trigger an immediate rebuild for every snap with a GitHub
    packaging repo that already has the automated build/publish workflow.

    Unlike the periodic Stale Build Scanner, this ignores publish staleness
    and doesn't create the workflow anywhere it's missing — it just fires a
    ``workflow_dispatch`` now for whatever's already there. Runs as a
    background agent (``RebuildAllSnapsAgent``); progress/result is visible
    on the Agents page.

    Called via ``fetch()`` from the Settings page so it doesn't reload the
    whole page — respond with JSON when ``X-Requested-With`` is present.
    Non-JS form submissions still get the old redirect fallback.
    """
    is_fetch = bool(request.headers.get("X-Requested-With"))

    user = get_current_user(request)
    if user is None:
        if is_fetch:
            return JSONResponse({"error": "not authenticated"}, status_code=401)
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]
    uc = get_user_config(user_id)
    if not uc or not (getattr(uc, "github_token", "") or ""):
        if is_fetch:
            return JSONResponse({"error": "rebuild_needs_github_token"}, status_code=400)
        return RedirectResponse(url="/settings?error=rebuild_needs_github_token", status_code=303)

    from snap_dashboard.agents.runner import get_runner
    from snap_dashboard.agents.stale_build_scanner import RebuildAllSnapsAgent

    get_runner().submit(RebuildAllSnapsAgent(user_id=user_id))
    if is_fetch:
        return JSONResponse({"started": True})
    return RedirectResponse(url="/agents", status_code=303)


@router.post("/settings/sync-snapcraft-credentials")
async def settings_sync_snapcraft_credentials(request: Request) -> RedirectResponse:
    """Push the stored Snapcraft Store credential out to every packaging repo.

    Runs as a background agent (see ``SnapcraftCredentialSyncAgent``) since
    it makes GitHub API calls per repo and could take a little while across
    the whole fleet — progress/result is visible on the Agents page.
    """
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]
    uc = get_user_config(user_id)
    if not uc or not getattr(uc, "snapcraft_macaroon", ""):
        return RedirectResponse(url="/settings?error=no_snapcraft_credential", status_code=303)

    from snap_dashboard.agents.snapcraft_credential_sync import SnapcraftCredentialSyncAgent
    from snap_dashboard.agents.runner import get_runner

    get_runner().submit(SnapcraftCredentialSyncAgent(user_id=user_id))
    return RedirectResponse(url="/agents", status_code=303)
