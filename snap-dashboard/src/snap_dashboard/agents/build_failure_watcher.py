"""Build failure watcher agent — detects a packaging repo's own
build/publish workflow failing on its default branch and dispatches the
configured coding backend to fix it.

This is distinct from ``agents/pr_monitor.py``'s ``auto_fix_ci_failures``,
which only watches PRs the bot itself opened (version-bump or dep_update
PRs). A packaging repo's build/publish workflow can start failing for all
sorts of reasons that have nothing to do with a bot PR — a Snap Store
API change, a base/build-snap update breaking a snapcraft.yaml part or
layout, an upstream dependency going away — and until now nothing noticed:
the snap would simply stop getting new revisions published, silently,
until a human happened to check the Actions tab.
"""

from __future__ import annotations

import logging

from snap_dashboard.agents.base import BaseAgent
from snap_dashboard.agents.coding_backend import get_coding_dispatcher, task_result_fields
from snap_dashboard.agents.stale_build_scanner import _dispatchable_workflows, _infer_build_workflow
from snap_dashboard.auth import get_user_config
from snap_dashboard.db.models import CopilotTask, Snap, User
from snap_dashboard.db.session import get_session
from snap_dashboard.github.bot_client import BotGitHubClient
from snap_dashboard.github.utils import is_owned_by, parse_owner_repo
from snap_dashboard.snapcraft.build_workflow_template import WORKFLOW_FILENAME, WORKFLOW_PATH

logger = logging.getLogger(__name__)


class BuildFailureWatcherAgent(BaseAgent):
    """Polls every owned packaging repo's build/publish workflow across all
    users and dispatches a ``build_fix`` Copilot task the first time it's
    seen failing on a given commit.

    Gated by ``UserConfig.auto_fix_build_failures`` (opt-in, off by
    default) — this opens a real PR against a real repo, same caution as
    every other auto-dispatch feature. Never re-dispatches for the same
    failing commit (``CopilotTask.dedupe_key`` holds the run's head SHA),
    so a fix attempt that itself failed, or is still in flight, doesn't
    get retried every poll — but a *new* failing commit (e.g. the repo
    owner pushed something else, or the fix PR was merged and something
    else broke) is treated as a fresh failure and does get a new attempt.
    """

    agent_type = "build_failure_watcher"

    def _run(self) -> str:
        with get_session() as session:
            q = session.query(User)
            if self.user_id:
                q = q.filter_by(id=self.user_id)
            user_ids = [u.id for u in q.all()]

        checked = 0
        dispatched = 0
        for uid in user_ids:
            uc = get_user_config(uid)
            if not uc or not getattr(uc, "auto_fix_build_failures", False):
                continue
            token = (getattr(uc, "bot_github_token", "") or getattr(uc, "github_token", "") or "")
            if not token:
                continue
            copilot = get_coding_dispatcher(uc)
            if not copilot:
                continue

            with get_session() as session:
                user = session.query(User).get(uid)
                login = (user.github_login or "") if user else ""
                snaps = [
                    (s.id, s.name, s.packaging_repo)
                    for s in session.query(Snap).filter_by(user_id=uid).all()
                    if s.packaging_repo
                ]
            snaps = [s for s in snaps if is_owned_by(s[2], login)]
            if not snaps:
                continue

            bot_client = BotGitHubClient(
                token,
                bot_login=getattr(uc, "bot_github_login", None),
                read_token=getattr(uc, "github_token", "") or token,
            )
            for snap_id, snap_name, packaging_repo in snaps:
                owner_repo = parse_owner_repo(packaging_repo)
                if not owner_repo:
                    continue
                owner, repo = owner_repo
                checked += 1
                self._report(f"Checking build status for {snap_name}", snap_name, user_id=uid)
                try:
                    if self._check_snap(bot_client, copilot, uid, snap_id, snap_name, owner, repo, uc):
                        dispatched += 1
                except Exception:
                    logger.exception("build_failure_watcher: failed checking %s/%s", owner, repo)

        return f"checked {checked} packaging repo(s), dispatched {dispatched} build_fix task(s)"

    def _check_snap(
        self,
        bot_client: BotGitHubClient,
        copilot,
        user_id: int,
        snap_id: int,
        snap_name: str,
        owner: str,
        repo: str,
        uc,
    ) -> bool:
        """Check one snap's packaging repo; dispatch a fix if it's newly failing."""
        owner_repo_str = f"{owner}/{repo}"
        default_branch = bot_client.get_default_branch(owner, repo)

        if bot_client.file_exists(owner, repo, WORKFLOW_PATH):
            workflow_file = WORKFLOW_FILENAME
        else:
            candidates = _dispatchable_workflows(bot_client, owner, repo)
            if not candidates:
                return False
            workflow_file, note = _infer_build_workflow(candidates, snap_name, uc)
            if note:
                logger.info("build_failure_watcher: %s — %s", snap_name, note)
            if not workflow_file:
                return False

        run = bot_client.latest_workflow_run(owner, repo, workflow_file, default_branch)
        if not run or run.get("conclusion") != "failure":
            return False

        head_sha = run.get("head_sha") or str(run.get("id"))
        with get_session() as session:
            existing = (
                session.query(CopilotTask)
                .filter(
                    CopilotTask.kind == "build_fix",
                    CopilotTask.owner_repo == owner_repo_str,
                    CopilotTask.dedupe_key == head_sha,
                )
                .first()
            )
            if existing:
                return False  # already attempted (or attempting) a fix for this exact commit

        jobs = bot_client.failed_job_summaries(owner, repo, run["id"])
        job_names = ", ".join(j["name"] for j in jobs) or "the build/publish workflow"
        log_blocks = "\n\n".join(
            f"### {j['name']} ({j['url']})\n```\n{j['log_tail']}\n```"
            for j in jobs
            if j["log_tail"]
        )
        prompt = (
            f"The `{workflow_file}` build/publish workflow is failing on the default branch "
            f"({default_branch}) of {owner_repo_str}, which packages the '{snap_name}' snap — "
            "it hasn't published a new revision since this started failing. "
            f"Failing job(s): {job_names}.\n\nRun: {run.get('html_url', '')}\n\n"
            + (f"Relevant log output:\n\n{log_blocks}\n\n" if log_blocks else "")
            + "Please look at the failure, fix whatever is causing the build/publish workflow "
            "to fail (e.g. a broken snapcraft.yaml part/layout, a stale patch, an out-of-date "
            "dependency pin, a packing/lint error), and open a pull request with the fix."
        )
        task = copilot.start_task(owner, repo, prompt, base_ref=default_branch, create_pull_request=True)
        with get_session() as session:
            session.add(
                CopilotTask(
                    user_id=user_id,
                    snap_id=snap_id,
                    kind="build_fix",
                    owner_repo=owner_repo_str,
                    prompt=prompt,
                    base_ref=default_branch,
                    dedupe_key=head_sha,
                    **task_result_fields(task, fallback_error=getattr(copilot, "last_error", None)),
                )
            )
        if task:
            logger.info(
                "build_failure_watcher: dispatched Copilot build_fix task for %s (run %s)",
                owner_repo_str, run.get("id"),
            )
        else:
            logger.warning(
                "build_failure_watcher: failed to dispatch Copilot build_fix task for %s (run %s)",
                owner_repo_str, run.get("id"),
            )
        return bool(task)
