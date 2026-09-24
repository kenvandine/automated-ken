"""PR monitor agent — polls version-bump PRs for CI, YARF, and auto-merge."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from snap_dashboard.agents.base import BaseAgent
from snap_dashboard.agents.coding_backend import get_coding_dispatcher, task_result_fields
from snap_dashboard.auth import get_user_config
from snap_dashboard.db.models import CopilotTask, TestRun, VersionBumpPR
from snap_dashboard.db.session import get_session
from snap_dashboard.github.utils import parse_owner_repo

logger = logging.getLogger(__name__)

_GH_API = "https://api.github.com"

# Statuses that mean "still waiting for something"
_IN_FLIGHT = {"open", "ci_pending", "ci_passed", "yarf_running", "agent_approved"}


def _gh_headers(token: str) -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


class PRMonitorAgent(BaseAgent):
    """Polls all in-flight VersionBumpPRs across all users and advances their status.

    State machine transitions:

    - open            → ci_pending       when a CI check run appears
    - ci_pending      → ci_passed/failed when all checks conclude
    - ci_passed       → yarf_running     triggers YARF test automatically
    - yarf_running    → yarf_passed/failed via existing TestRun sync
    - yarf_*/needs_rv → agent_approved/rejected/needs_review/promoting
                         spawns ScreenshotReviewerAgent
    - agent_approved  → merged           when UserConfig.auto_merge is True

    Also syncs PRs that were closed or merged directly on GitHub so the DB
    never gets stuck in a stale state.
    """

    agent_type = "pr_monitor"

    def __init__(self, user_id: int | None = None) -> None:
        super().__init__(user_id=user_id)

    def _run(self) -> str:
        with get_session() as session:
            q = session.query(VersionBumpPR).filter(VersionBumpPR.status.in_(_IN_FLIGHT))
            if self.user_id:
                q = q.filter_by(user_id=self.user_id)
            prs = [
                {
                    "id": p.id,
                    "snap_id": p.snap_id,
                    "user_id": p.user_id,
                    "status": p.status,
                    "packaging_repo": p.packaging_repo or "",
                    "bot_pr_number": p.bot_pr_number,
                    "bot_pr_url": p.bot_pr_url or "",
                    "branch_name": p.branch_name or "",
                    "test_run_id": p.test_run_id,
                    "new_version": p.new_version or "",
                    "old_version": p.old_version or "",
                }
                for p in q.all()
            ]

        self._report(f"Polling {len(prs)} in-flight version bump PR(s)…")
        updated = 0
        for pr in prs:
            try:
                if self._advance(pr):
                    updated += 1
            except Exception as exc:
                logger.warning("pr_monitor: error on PR %s: %s", pr["id"], exc)

        return f"checked {len(prs)} in-flight PRs, advanced {updated}"

    def _advance(self, pr: dict) -> bool:
        """Try to advance the PR state machine; return True if state changed."""
        uc = get_user_config(pr["user_id"]) if pr["user_id"] else None
        token = (uc.github_token if uc else "") or ""

        status = pr["status"]
        pkg_repo = pr["packaging_repo"]
        pr_number = pr["bot_pr_number"]

        if not pkg_repo or not pr_number:
            return False

        owner_repo = parse_owner_repo(pkg_repo) if pkg_repo else None
        if not owner_repo:
            return False
        owner, repo = owner_repo

        if status == "open":
            # Always check whether the PR was closed/merged on GitHub first.
            if self._check_pr_closed(pr, owner, repo, token):
                return True
            return self._check_ci_start(pr, owner, repo, token)
        if status == "ci_pending":
            if self._check_pr_closed(pr, owner, repo, token):
                return True
            return self._check_ci_complete(pr, owner, repo, token, uc)
        if status == "ci_passed":
            return self._trigger_yarf(pr, uc)
        if status == "yarf_running":
            return self._check_yarf(pr)
        if status == "agent_approved":
            return self._check_auto_merge(pr, uc, owner, repo, token)
        return False

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def _check_pr_closed(self, pr: dict, owner: str, repo: str, token: str) -> bool:
        """Detect PRs closed or merged on GitHub and sync the DB status.

        Returns True if the status was updated.
        """
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.get(
                    f"{_GH_API}/repos/{owner}/{repo}/pulls/{pr['bot_pr_number']}",
                    headers=_gh_headers(token),
                )
            if resp.status_code != 200:
                return False
            data = resp.json()
            if data.get("merged"):
                _update_pr_status_merged(pr["id"])
                snap_name = _snap_name_from_id(pr["snap_id"]) or "?"
                logger.info(
                    "pr_monitor: PR #%s for %s was merged on GitHub — syncing",
                    pr["bot_pr_number"], snap_name,
                )
                return True
            if data.get("state") == "closed":
                _update_pr_status(pr["id"], "closed")
                return True
        except Exception as exc:
            logger.debug("_check_pr_closed failed for PR %s: %s", pr["id"], exc)
        return False

    def _check_ci_start(self, pr: dict, owner: str, repo: str, token: str) -> bool:
        """open → ci_pending when a check run exists for the PR."""
        runs = _get_pr_check_runs(owner, repo, pr["bot_pr_number"], token)
        if runs:
            _update_pr_status(pr["id"], "ci_pending")
            return True
        return False

    def _check_ci_complete(self, pr: dict, owner: str, repo: str, token: str, uc) -> bool:
        """ci_pending → ci_passed/ci_failed when all checks conclude."""
        runs = _get_pr_check_runs(owner, repo, pr["bot_pr_number"], token)
        if not runs:
            return False
        conclusions = [r.get("conclusion") for r in runs if r.get("status") == "completed"]
        if len(conclusions) < len(runs):
            return False  # still running
        if all(c == "success" for c in conclusions):
            _update_pr_status(pr["id"], "ci_passed")
        else:
            _update_pr_status(pr["id"], "ci_failed")
            self._maybe_dispatch_ci_fix(pr, owner, repo, uc, runs)
        return True

    def _maybe_dispatch_ci_fix(self, pr: dict, owner: str, repo: str, uc, runs: list[dict]) -> None:
        """ci_failed → dispatch the configured coding backend to open a fix PR.

        Opt-in via UserConfig.auto_fix_ci_failures — this delegates real
        code-editing work to whichever "capable coding" backend is configured
        (see agents/coding_backend.py; GitHub Copilot cloud agent today,
        pending a capable local model), rather than the local Lemonade model,
        which is only used for lightweight text/vision tasks elsewhere.
        """
        if not uc or not getattr(uc, "auto_fix_ci_failures", False):
            return
        client = get_coding_dispatcher(uc)
        if not client:
            return
        owner_repo = f"{owner}/{repo}"
        with get_session() as session:
            existing = (
                session.query(CopilotTask)
                .filter(
                    CopilotTask.kind == "ci_fix",
                    CopilotTask.owner_repo == owner_repo,
                    CopilotTask.issue_number == pr["bot_pr_number"],
                    CopilotTask.status.in_(["queued", "in_progress"]),
                )
                .first()
            )
            if existing:
                return  # already dispatched, avoid duplicate tasks

        failed = [r for r in runs if r.get("conclusion") not in ("success", None)]
        failed_names = ", ".join(r.get("name", "?") for r in failed) or "the CI checks"
        failed_urls = "\n".join(f"- {r.get('name', '?')}: {r.get('html_url', '')}" for r in failed)
        head_branch = pr.get("branch_name") or ""
        snap_name = _snap_name_from_id(pr["snap_id"]) or "this snap"
        prompt = (
            f"The build/test workflow failed on PR #{pr['bot_pr_number']} in {owner_repo}, "
            f"which bumps {snap_name} to version {pr.get('new_version')}. "
            f"Failing check(s): {failed_names}.\n{failed_urls}\n\n"
            "Please look at the failure logs, fix whatever is causing the build/test "
            "workflow to fail (e.g. a broken snapcraft.yaml part, a stale patch, an "
            "out-of-date dependency pin), and open a pull request with the fix."
        )
        task = client.start_task(
            owner, repo, prompt, base_ref=head_branch or "main", create_pull_request=True,
        )
        with get_session() as session:
            session.add(
                CopilotTask(
                    user_id=pr.get("user_id"),
                    snap_id=pr.get("snap_id"),
                    kind="ci_fix",
                    owner_repo=owner_repo,
                    prompt=prompt,
                    issue_number=pr["bot_pr_number"],
                    **task_result_fields(task),
                )
            )
        if task:
            logger.info("pr_monitor: dispatched Copilot ci_fix task for %s PR #%s", owner_repo, pr["bot_pr_number"])
        else:
            logger.warning("pr_monitor: failed to dispatch Copilot ci_fix task for %s PR #%s", owner_repo, pr["bot_pr_number"])

    def _trigger_yarf(self, pr: dict, uc) -> bool:
        """ci_passed → yarf_running by queuing one YARF test run per architecture.

        Every architecture the snap actually ships (see
        ``orchestrator.get_snap_architectures``) gets its own ``TestRun``,
        tagged with this PR via ``version_bump_pr_id`` — e.g. one for an
        amd64 runner and one for an arm64 runner to pick up independently.
        ``_check_yarf`` waits for all of them before advancing the PR, and
        promotion later releases every architecture's revision together.
        """
        snap_name = _snap_name_from_id(pr["snap_id"])
        if not snap_name:
            return False

        from snap_dashboard.testing.orchestrator import get_snap_architectures, trigger_remote_run
        with get_session() as session:
            archs = get_snap_architectures(session, pr["snap_id"])

        self._report(
            f"Queuing YARF test for {snap_name} {pr['new_version']} ({', '.join(archs)})",
            snap_name,
        )

        # Tests run on registered remote runners (real hardware polling this
        # dashboard), not GitHub Actions — see snap_dashboard.db.models.Runner.
        run_ids: list[int] = []
        errors: list[str] = []
        for arch in archs:
            ok, err, run_id = trigger_remote_run(
                snap_name=snap_name,
                from_channel="edge",
                version=pr["new_version"],
                revision=None,
                architecture=arch,
                triggered_by="auto",
                user_id=pr["user_id"],
            )
            if ok and run_id:
                run_ids.append(run_id)
            else:
                errors.append(f"{arch}: {err}")

        if not run_ids:
            logger.warning("pr_monitor: YARF trigger failed for PR %s: %s", pr["id"], "; ".join(errors))
            return False
        if errors:
            logger.warning(
                "pr_monitor: YARF trigger partially failed for PR %s: %s", pr["id"], "; ".join(errors)
            )

        with get_session() as session:
            for run_id in run_ids:
                run = session.query(TestRun).get(run_id)
                if run:
                    run.version_bump_pr_id = pr["id"]
            bump = session.query(VersionBumpPR).get(pr["id"])
            if bump:
                bump.status = "yarf_running"
                bump.test_run_id = run_ids[0]  # representative run for legacy single-run displays
        return True

    def _check_yarf(self, pr: dict) -> bool:
        """yarf_running → yarf_passed/yarf_failed once every architecture's run finishes."""
        with get_session() as session:
            runs = session.query(TestRun).filter_by(version_bump_pr_id=pr["id"]).all()
            if not runs:
                # Pre-migration PR with no sibling rows recorded — fall back
                # to the single representative run.
                if not pr["test_run_id"]:
                    return False
                run = session.query(TestRun).get(pr["test_run_id"])
                runs = [run] if run else []
            if not runs:
                return False
            if not all(r.status in ("passed", "failed") for r in runs):
                return False  # still waiting on at least one architecture
            new_status = "yarf_passed" if all(r.status == "passed" for r in runs) else "yarf_failed"
            _update_pr_status(pr["id"], new_status)

        # Spawn one screenshot reviewer per architecture's run.
        self._spawn_reviewer(pr, [r.id for r in runs])
        return True

    def _check_auto_merge(self, pr: dict, uc, owner: str, repo: str, token: str) -> bool:
        """agent_approved → merged when UserConfig.auto_merge is enabled.

        Uses the user's primary GitHub token (not the bot token) to merge,
        so the merge appears under the maintainer's account.
        """
        if not uc or not getattr(uc, "auto_merge", False):
            return False
        if not token:
            return False
        if not pr["bot_pr_number"]:
            return False

        snap_name = _snap_name_from_id(pr["snap_id"]) or "?"
        self._report(
            f"Auto-merging {snap_name} {pr['new_version']} (agent approved)…",
            snap_name,
        )

        from snap_dashboard.testing.promoter import merge_packaging_pr

        if merge_packaging_pr(pr["packaging_repo"], pr["bot_pr_number"], token):
            _update_pr_status_merged(pr["id"])
            logger.info(
                "pr_monitor: auto-merged PR #%s for %s %s→%s",
                pr["bot_pr_number"], snap_name,
                pr["old_version"], pr["new_version"],
            )
            return True
        return False

    def _spawn_reviewer(self, pr: dict, test_run_ids: list[int]) -> None:
        from snap_dashboard.agents.screenshot_reviewer import ScreenshotReviewerAgent
        from snap_dashboard.agents.runner import get_runner
        runner = get_runner()
        for test_run_id in test_run_ids:
            runner.submit(
                ScreenshotReviewerAgent(
                    version_bump_pr_id=pr["id"],
                    test_run_id=test_run_id,
                    user_id=pr["user_id"],
                )
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _update_pr_status(pr_id: int, status: str) -> None:
    with get_session() as session:
        bump = session.query(VersionBumpPR).get(pr_id)
        if bump:
            bump.status = status


def _update_pr_status_merged(pr_id: int) -> None:
    with get_session() as session:
        bump = session.query(VersionBumpPR).get(pr_id)
        if bump:
            bump.status = "merged"
            bump.merged_at = datetime.now(timezone.utc)


def _get_pr_check_runs(owner: str, repo: str, pr_number: int, token: str) -> list[dict]:
    """Return check runs for the HEAD commit of a PR."""
    try:
        with httpx.Client(timeout=15) as client:
            pr_resp = client.get(
                f"{_GH_API}/repos/{owner}/{repo}/pulls/{pr_number}",
                headers=_gh_headers(token),
            )
        if pr_resp.status_code != 200:
            return []
        head_sha = pr_resp.json().get("head", {}).get("sha", "")
        if not head_sha:
            return []
        with httpx.Client(timeout=15) as client:
            runs_resp = client.get(
                f"{_GH_API}/repos/{owner}/{repo}/commits/{head_sha}/check-runs",
                headers=_gh_headers(token),
            )
        if runs_resp.status_code != 200:
            return []
        return runs_resp.json().get("check_runs", [])
    except Exception as exc:
        logger.debug("_get_pr_check_runs failed: %s", exc)
        return []


def _snap_name_from_id(snap_id: int) -> str | None:
    from snap_dashboard.db.models import Snap
    with get_session() as session:
        snap = session.query(Snap).get(snap_id)
        return snap.name if snap else None
