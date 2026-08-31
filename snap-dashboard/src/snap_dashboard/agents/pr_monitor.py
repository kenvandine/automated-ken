"""PR monitor agent — polls version-bump PRs for CI, YARF, and auto-merge."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from snap_dashboard.agents.base import BaseAgent
from snap_dashboard.auth import get_user_config
from snap_dashboard.db.models import VersionBumpPR
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
            return self._check_ci_complete(pr, owner, repo, token)
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

    def _check_ci_complete(self, pr: dict, owner: str, repo: str, token: str) -> bool:
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
        return True

    def _trigger_yarf(self, pr: dict, uc) -> bool:
        """ci_passed → yarf_running by triggering a YARF test run."""
        if not uc or not uc.testing_repo or not uc.github_token:
            return False
        snap_name = _snap_name_from_id(pr["snap_id"])
        if snap_name:
            self._report(f"Triggering YARF test for {snap_name} {pr['new_version']}", snap_name)
        if not snap_name:
            return False
        from snap_dashboard.testing.orchestrator import trigger_workflow
        ok, err, run_id = trigger_workflow(
            snap_name=snap_name,
            from_channel="edge",
            version=pr["new_version"],
            revision=None,
            triggered_by="auto",
            testing_repo=uc.testing_repo,
            github_token=uc.github_token,
            user_id=pr["user_id"],
        )
        if ok and run_id:
            with get_session() as session:
                bump = session.query(VersionBumpPR).get(pr["id"])
                if bump:
                    bump.status = "yarf_running"
                    bump.test_run_id = run_id
            return True
        logger.warning("pr_monitor: YARF trigger failed for PR %s: %s", pr["id"], err)
        return False

    def _check_yarf(self, pr: dict) -> bool:
        """yarf_running → yarf_passed/yarf_failed via existing TestRun record."""
        if not pr["test_run_id"]:
            return False
        from snap_dashboard.db.models import TestRun
        with get_session() as session:
            run = session.query(TestRun).get(pr["test_run_id"])
            if not run:
                return False
            if run.status not in ("passed", "failed"):
                return False
            new_status = "yarf_passed" if run.status == "passed" else "yarf_failed"
            _update_pr_status(pr["id"], new_status)

        # Spawn screenshot reviewer
        self._spawn_reviewer(pr)
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

    def _spawn_reviewer(self, pr: dict) -> None:
        from snap_dashboard.agents.screenshot_reviewer import ScreenshotReviewerAgent
        from snap_dashboard.agents.runner import get_runner
        reviewer = ScreenshotReviewerAgent(
            version_bump_pr_id=pr["id"],
            test_run_id=pr["test_run_id"],
            user_id=pr["user_id"],
        )
        get_runner().submit(reviewer)


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
