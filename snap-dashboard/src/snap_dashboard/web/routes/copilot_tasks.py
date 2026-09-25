"""Dashboard visibility for delegated Copilot cloud agent tasks.

Every ``CopilotTask`` row is a task dispatched to whatever "capable coding"
backend is configured (see ``agents/coding_backend.py``) — CI fixes,
dependency upgrades, issue fixes, and fleet-normalization. This page lists
them all so a user can see what automated-ken has asked Copilot to do on
their behalf, and refresh each task's live status/PR link from GitHub.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from snap_dashboard.agents.coding_backend import (
    RETRY_ELIGIBLE_STATUSES,
    extract_pr_url,
    get_coding_dispatcher,
    task_result_fields,
)
from snap_dashboard.auth import get_current_user, get_user_config
from snap_dashboard.db.models import CopilotTask
from snap_dashboard.db.session import get_session
from snap_dashboard.github.copilot_agent import CopilotAgentClient
from snap_dashboard.github.utils import parse_owner_repo
from snap_dashboard.web.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()

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
        "can_retry": (task.status or "") in RETRY_ELIGIBLE_STATUSES and bool(task.prompt),
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


@router.post("/copilot-tasks/{task_id}/retry")
async def retry_copilot_task(task_id: int, request: Request) -> RedirectResponse:
    """Re-dispatch a failed task as a brand-new ``CopilotTask`` row.

    Failed dispatches (dispatch_failed/failed/cancelled/timed_out) previously
    had no way back — the dedup checks in agents/repo_normalizer.py and
    agents/upstream_maintainer.py treated *any* existing row as "already
    handled", so a transient failure (no Copilot license, a network blip)
    would skip the repo/issue forever until manually deleted from the DB.
    This inserts a fresh row (preserving the old one for history/audit)
    using the same prompt/base_ref/kind originally dispatched.
    """
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]
    with get_session() as session:
        original = session.query(CopilotTask).filter_by(id=task_id, user_id=user_id).first()
        if not original or not original.prompt or not original.owner_repo:
            return RedirectResponse(url="/copilot-tasks", status_code=303)
        owner_repo = original.owner_repo
        prompt = original.prompt
        base_ref = original.base_ref or "main"
        kind = original.kind
        snap_id = original.snap_id
        issue_number = original.issue_number

    owner_repo_parts = parse_owner_repo(owner_repo)
    if not owner_repo_parts:
        return RedirectResponse(url="/copilot-tasks", status_code=303)
    owner, repo = owner_repo_parts

    uc = get_user_config(user_id)
    dispatcher = get_coding_dispatcher(uc)
    if dispatcher is None:
        return RedirectResponse(url="/copilot-tasks", status_code=303)

    task = dispatcher.start_task(owner, repo, prompt, base_ref=base_ref, create_pull_request=True)
    with get_session() as session:
        session.add(
            CopilotTask(
                user_id=user_id,
                snap_id=snap_id,
                kind=kind,
                owner_repo=owner_repo,
                prompt=prompt,
                issue_number=issue_number,
                base_ref=base_ref,
                **task_result_fields(task, fallback_error=getattr(dispatcher, "last_error", None)),
            )
        )

    return RedirectResponse(url="/copilot-tasks", status_code=303)
