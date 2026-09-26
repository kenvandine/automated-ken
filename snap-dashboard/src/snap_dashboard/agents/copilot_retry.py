"""Background agent that re-dispatches a failed ``CopilotTask``.

``web/routes/copilot_tasks.py``'s Retry action used to call
``dispatcher.start_task()`` directly, inline, inside its ``async def`` route
handler. That's a blocking call — it can hit GitHub's REST API, wait ~20s+
for the local Lemonade model to reply, and (since the fork-based PR fix)
poll for up to ~30s for a freshly-created fork to become ready — and
FastAPI runs ``async def`` routes on the single asyncio event loop thread,
not a worker thread. Blocking that thread for tens of seconds froze the
*entire* web UI for every user, not just the one who clicked Retry.

This wraps the same work as a ``BaseAgent`` subclass instead, so it runs on
the existing agent thread pool (see ``agents/runner.py``) exactly like every
other coding-agent dispatch, with the same full log capture the "Agent
Logs" page already provides for every other agent run.
"""

from __future__ import annotations

import logging

from snap_dashboard.agents.base import BaseAgent
from snap_dashboard.agents.coding_backend import get_coding_dispatcher, task_result_fields
from snap_dashboard.auth import get_user_config
from snap_dashboard.db.models import CopilotTask
from snap_dashboard.db.session import get_session

logger = logging.getLogger(__name__)


class RetryCopilotTaskAgent(BaseAgent):
    """Re-runs ``start_task()`` for one ``CopilotTask`` row and updates it in place."""

    agent_type = "copilot_retry"

    def __init__(
        self,
        user_id: int,
        task_id: int,
        owner: str,
        repo: str,
        prompt: str,
        base_ref: str,
    ) -> None:
        super().__init__(user_id=user_id, snap_name=f"{owner}/{repo}")
        self.task_id = task_id
        self.owner = owner
        self.repo = repo
        self.prompt = prompt
        self.base_ref = base_ref

    def _run(self) -> str:
        uc = get_user_config(self.user_id)
        dispatcher = get_coding_dispatcher(uc)
        if dispatcher is None:
            fields = {
                "external_task_id": None,
                "status": "dispatch_failed",
                "pr_url": None,
                "error_msg": "No coding backend configured.",
            }
        else:
            task = dispatcher.start_task(
                self.owner, self.repo, self.prompt, base_ref=self.base_ref, create_pull_request=True,
            )
            fields = task_result_fields(task, fallback_error=getattr(dispatcher, "last_error", None))

        with get_session() as session:
            row = session.query(CopilotTask).filter_by(id=self.task_id).first()
            if row:
                for key, value in fields.items():
                    setattr(row, key, value)

        return f"retry status={fields['status']}"
