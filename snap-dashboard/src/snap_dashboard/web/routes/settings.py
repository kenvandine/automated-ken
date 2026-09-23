"""Settings page routes."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from snap_dashboard.auth import get_current_user, get_user_config
from snap_dashboard.db.models import CollectionRun, Snap, UserConfig
from snap_dashboard.db.session import get_session

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
        "settings.html",
        {
            "request": request,
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
    publisher: str = Form(default=""),
    github_token: str = Form(default=""),
    interval: int = Form(default=6),
    testing_repo: str = Form(default=""),
    auto_test: str = Form(default=""),
    lemonade_server_url: str = Form(default=""),
    lemonade_model: str = Form(default=""),
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
    """Save per-user settings to UserConfig in the database."""
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]
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
        if publisher.strip():
            uc.publisher = publisher.strip()
        if github_token.strip():
            uc.github_token = github_token.strip()
        uc.collect_interval_hours = interval
        uc.testing_repo = testing_repo.strip()
        uc.auto_test = _auto_test
        # Agent / AI settings
        if lemonade_server_url.strip():
            uc.lemonade_server_url = lemonade_server_url.strip()
        if lemonade_model.strip():
            uc.lemonade_model = lemonade_model.strip()
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
async def settings_remove_snap(snap_name: str, request: Request) -> RedirectResponse:
    """Remove a snap from tracking."""
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]

    with get_session() as session:
        snap = session.query(Snap).filter_by(name=snap_name, user_id=user_id).first()
        if snap:
            session.delete(snap)

    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/run-fleet-normalization")
async def settings_run_fleet_normalization(request: Request) -> RedirectResponse:
    """Manually trigger the one-time fleet-normalization campaign.

    Unlike the periodic agents, this is a deliberate one-off run — a user
    clicks this after enabling ``fleet_normalization_enabled`` to kick off
    the pass across all packaging repos.
    """
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]
    uc = get_user_config(user_id)
    if not uc or not getattr(uc, "fleet_normalization_enabled", False):
        return RedirectResponse(url="/settings?error=fleet_normalization_disabled", status_code=303)

    from snap_dashboard.agents.repo_normalizer import RepoNormalizerAgent
    from snap_dashboard.agents.runner import get_runner

    get_runner().submit(RepoNormalizerAgent(user_id=user_id))
    return RedirectResponse(url="/agents", status_code=303)
