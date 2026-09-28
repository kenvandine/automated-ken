"""Custom prompt agent — user-authored freeform coding-task dispatch.

Lets a user type an arbitrary instruction on a snap's detail page (e.g.
"there's a new 0.5.0 upstream release which needs core24/gnome-46-2404 —
update snapcraft.yaml and anything else needed") and have it dispatched to
the configured coding backend against that snap's packaging repo, the same
way the other one-off dispatch agents work (``StackUpdateAgent``,
``RepoNormalizerAgent``'s single-snap mode).

Uses ``CopilotTask.kind == "custom_prompt"`` so
``PRMonitorAgent._check_dep_update_prs`` (which watches both ``dep_update``
and ``custom_prompt`` PRs generically) picks up the resulting PR, watches
its build/test workflow, and dispatches a follow-up ``ci_fix`` task if it
fails — opt-in via the same ``UserConfig.auto_fix_ci_failures`` setting
used everywhere else.
"""

from __future__ import annotations

import logging

from snap_dashboard.agents.base import BaseAgent
from snap_dashboard.agents.coding_backend import get_coding_dispatcher, task_result_fields
from snap_dashboard.auth import get_user_config
from snap_dashboard.db.models import CopilotTask, Snap
from snap_dashboard.db.session import get_session
from snap_dashboard.github.bot_client import BotGitHubClient
from snap_dashboard.github.utils import parse_owner_repo

logger = logging.getLogger(__name__)


class CustomPromptAgent(BaseAgent):
    """One-off dispatch of a user-typed instruction against a snap's packaging repo."""

    agent_type = "custom_prompt"

    def __init__(
        self,
        user_id: int | None = None,
        snap_id: int | None = None,
        prompt: str = "",
        snap_name: str | None = None,
    ) -> None:
        super().__init__(user_id=user_id, snap_name=snap_name)
        self.snap_id = snap_id
        self.user_prompt = prompt

    def _run(self) -> str:
        if not self.user_id or not self.snap_id:
            return "no user_id/snap_id — skipped"
        if not self.user_prompt.strip():
            return "empty prompt — skipped"
        uc = get_user_config(self.user_id)
        if not uc:
            return "no user config"
        token = getattr(uc, "bot_github_token", "") or getattr(uc, "github_token", "") or ""
        if not token:
            return "no GitHub token configured"
        read_token = getattr(uc, "github_token", "") or token

        with get_session() as session:
            snap = session.query(Snap).filter_by(id=self.snap_id, user_id=self.user_id).first()
            if not snap:
                return "snap not found"
            snap_name = snap.name
            packaging_repo = snap.packaging_repo

        if not packaging_repo:
            return "no packaging repo configured"

        owner_repo = parse_owner_repo(packaging_repo)
        if not owner_repo:
            return "could not parse packaging repo"
        owner, repo = owner_repo
        owner_repo_str = f"{owner}/{repo}"

        self._report(f"Dispatching custom task for {owner_repo_str}", snap_name)

        dispatcher = get_coding_dispatcher(uc)
        if not dispatcher:
            return "no coding backend configured/available"

        bot_client = BotGitHubClient(
            token, bot_login=getattr(uc, "bot_github_login", None), read_token=read_token
        )
        base_ref = bot_client.get_default_branch(owner, repo)

        prompt = (
            f"This is the packaging repo for the '{snap_name}' snap. A maintainer has "
            f"requested the following:\n\n{self.user_prompt.strip()}\n\n"
            "Make all the changes necessary (snapcraft.yaml and any other affected files), "
            "then open a pull request with the changes."
        )

        task = dispatcher.start_task(owner, repo, prompt, base_ref=base_ref, create_pull_request=True)
        fields = task_result_fields(task, fallback_error=getattr(dispatcher, "last_error", None))
        with get_session() as session:
            session.add(
                CopilotTask(
                    user_id=self.user_id,
                    snap_id=self.snap_id,
                    kind="custom_prompt",
                    owner_repo=owner_repo_str,
                    prompt=prompt,
                    base_ref=base_ref,
                    **fields,
                )
            )

        # Make the summary self-contained: whether it succeeded, is still
        # queued/running, or failed, and (if known already) the PR url —
        # this is what shows up both in the "recent activity" list and as
        # the final line of the persisted run log, so it needs to answer
        # "did this work?" without requiring a click into /copilot-tasks.
        status = fields.get("status")
        pr_url = fields.get("pr_url")
        error_msg = fields.get("error_msg")
        if status == "failed" or (not task and status == "dispatch_failed"):
            detail = f" — {error_msg}" if error_msg else ""
            return f"FAILED: custom task for {owner_repo_str}{detail}"
        if status == "completed" and pr_url:
            return f"succeeded: opened {pr_url}"
        if status == "completed":
            return f"succeeded for {owner_repo_str} (no PR url returned)"
        # copilot_cloud_agent dispatches async — status is "queued" here;
        # the real outcome (and PR url) shows up later in /copilot-tasks
        # and, once the PR opens, in pr_monitor.py's CI-watch.
        return f"dispatched custom task for {owner_repo_str} (status={status or 'unknown'})"
