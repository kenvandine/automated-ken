"""Dashboard visibility for delegated coding tasks.

Every ``CopilotTask`` row (legacy table name) is a task dispatched to the
configured "capable coding" backend (see ``agents/coding_backend.py``) — CI
fixes, dependency upgrades, issue fixes, and fleet-normalization. This page
lists them all so a user can see what automated-ken has done on their behalf,
and retry failed ones.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from snap_dashboard.agents.coding_backend import RETRY_ELIGIBLE_STATUSES
from snap_dashboard.auth import get_current_user
from snap_dashboard.db.models import CopilotTask
from snap_dashboard.db.session import get_session
from snap_dashboard.github.utils import parse_owner_repo
from snap_dashboard.web.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()

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
        "ci_status": task.ci_status,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "is_terminal": (task.status or "") in _TERMINAL_STATUSES,
        "can_retry": (task.status or "") in RETRY_ELIGIBLE_STATUSES and bool(task.prompt),
    }


@router.get("/copilot-tasks")
async def copilot_tasks_legacy() -> RedirectResponse:
    return RedirectResponse(url="/agents/tasks", status_code=301)


@router.get("/agents/tasks", response_class=HTMLResponse)
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

    The actual ``start_task()`` call can block for tens of seconds (the
    local Lemonade model replying, or forking+polling a repo for the
    fork-based PR flow — see ``github/fork_utils.py``), so it's dispatched
    onto the background agent thread pool (``RetryCopilotTaskAgent``)
    instead of being awaited inline here — this route only does two quick
    DB reads/writes and returns immediately. Doing the blocking call inline
    in this ``async def`` route previously froze the *entire* web UI (every
    user's requests, not just this one) for as long as the dispatch took,
    since FastAPI runs ``async def`` handlers on the single event-loop
    thread.
    """
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/auth/login", status_code=302)

    user_id = user["id"]
    with get_session() as session:
        original = session.query(CopilotTask).filter_by(id=task_id, user_id=user_id).first()
        if not original or not original.prompt or not original.owner_repo:
            return RedirectResponse(url="/agents/tasks", status_code=303)
        owner_repo = original.owner_repo
        prompt = original.prompt
        base_ref = original.base_ref or "main"
        kind = original.kind
        snap_id = original.snap_id
        issue_number = original.issue_number

    owner_repo_parts = parse_owner_repo(owner_repo)
    if not owner_repo_parts:
        return RedirectResponse(url="/agents/tasks", status_code=303)
    owner, repo = owner_repo_parts

    with get_session() as session:
        new_task = CopilotTask(
            user_id=user_id,
            snap_id=snap_id,
            kind=kind,
            owner_repo=owner_repo,
            prompt=prompt,
            issue_number=issue_number,
            base_ref=base_ref,
            status="dispatching",
        )
        session.add(new_task)
        session.flush()
        new_task_id = new_task.id

    from snap_dashboard.agents.copilot_retry import RetryCopilotTaskAgent
    from snap_dashboard.agents.runner import get_runner

    get_runner().submit(
        RetryCopilotTaskAgent(
            user_id=user_id, task_id=new_task_id, owner=owner, repo=repo, prompt=prompt, base_ref=base_ref,
        )
    )

    return RedirectResponse(url="/agents/tasks", status_code=303)
