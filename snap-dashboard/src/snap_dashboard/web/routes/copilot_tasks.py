"""Dashboard visibility for delegated Copilot cloud agent tasks.

Every ``CopilotTask`` row is a task dispatched to whatever "capable coding"
backend is configured (see ``agents/coding_backend.py``) — CI fixes,
dependency upgrades, issue fixes, and fleet-normalization. This page lists
them all so a user can see what automated-ken has asked Copilot to do on
their behalf, and refresh each task's live status/PR link from GitHub.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from snap_dashboard.agents.coding_backend import extract_pr_url
from snap_dashboard.auth import get_current_user, get_user_config
from snap_dashboard.db.models import CopilotTask
from snap_dashboard.db.session import get_session
from snap_dashboard.github.copilot_agent import CopilotAgentClient
from snap_dashboard.github.utils import parse_owner_repo

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

# States GitHub's Copilot cloud agent task API reports.
_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "timed_out", "dispatch_failed"}


def _serialise(task: CopilotTask) -> dict:
    return {
        "id": task.id,
        "kind": task.kind,
        "owner_repo": task.owner_repo,
        "status": task.status,
        "pr_url": task.pr_url,
        "issue_number": task.issue_number,
        "error_msg": task.error_msg,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "is_terminal": (task.status or "") in _TERMINAL_STATUSES,
    }


@router.get("/copilot-tasks", response_class=HTMLResponse)
async def copilot_tasks_page(request: Request) -> HTMLResponse:
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]
    with get_session() as session:
        tasks = (
            session.query(CopilotTask)
            .filter_by(user_id=user_id)
            .order_by(CopilotTask.created_at.desc())
            .all()
        )
        task_list = [_serialise(t) for t in tasks]

    kinds = sorted({t["kind"] for t in task_list})

    return templates.TemplateResponse(
        request,
        "copilot_tasks.html",
        {
            "tasks": task_list,
            "kinds": kinds,
            "current_user": user,
        },
    )


@router.post("/copilot-tasks/{task_id}/refresh")
async def refresh_copilot_task(task_id: int, request: Request) -> RedirectResponse:
    """Poll GitHub for a single task's current status/PR, and store it."""
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]
    uc = get_user_config(user_id)
    token = (getattr(uc, "bot_github_token", "") or getattr(uc, "github_token", "") or "") if uc else ""

    if token:
        with get_session() as session:
            task = session.query(CopilotTask).filter_by(id=task_id, user_id=user_id).first()
            owner_repo = task.owner_repo if task else None
            external_task_id = task.external_task_id if task else None

        if owner_repo and external_task_id:
            owner_repo_parts = parse_owner_repo(owner_repo)
            if owner_repo_parts:
                owner, repo = owner_repo_parts
                remote = CopilotAgentClient(token).get_task(owner, repo, external_task_id)
                if remote:
                    with get_session() as session:
                        task = session.query(CopilotTask).filter_by(id=task_id, user_id=user_id).first()
                        if task:
                            task.status = remote.get("state") or task.status
                            task.pr_url = extract_pr_url(remote) or task.pr_url
                            if remote.get("error"):
                                task.error_msg = str(remote.get("error"))[:2000]

    return RedirectResponse(url="/copilot-tasks", status_code=303)


@router.post("/copilot-tasks/refresh-all")
async def refresh_all_copilot_tasks(request: Request) -> RedirectResponse:
    """Poll GitHub for every non-terminal task's status/PR in one pass."""
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]
    uc = get_user_config(user_id)
    token = (getattr(uc, "bot_github_token", "") or getattr(uc, "github_token", "") or "") if uc else ""
    if not token:
        return RedirectResponse(url="/copilot-tasks", status_code=303)

    with get_session() as session:
        pending = [
            (t.id, t.owner_repo, t.external_task_id)
            for t in session.query(CopilotTask)
            .filter_by(user_id=user_id)
            .filter(~CopilotTask.status.in_(_TERMINAL_STATUSES))
            .all()
            if t.owner_repo and t.external_task_id
        ]

    client = CopilotAgentClient(token)
    for task_id, owner_repo, external_task_id in pending:
        owner_repo_parts = parse_owner_repo(owner_repo)
        if not owner_repo_parts:
            continue
        owner, repo = owner_repo_parts
        remote = client.get_task(owner, repo, external_task_id)
        if not remote:
            continue
        with get_session() as session:
            task = session.query(CopilotTask).filter_by(id=task_id, user_id=user_id).first()
            if task:
                task.status = remote.get("state") or task.status
                task.pr_url = extract_pr_url(remote) or task.pr_url
                if remote.get("error"):
                    task.error_msg = str(remote.get("error"))[:2000]

    return RedirectResponse(url="/copilot-tasks", status_code=303)
