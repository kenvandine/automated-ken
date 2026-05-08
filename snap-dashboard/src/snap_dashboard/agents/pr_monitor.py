"""PR monitor agent — polls version-bump PRs for CI and YARF status."""

from __future__ import annotations

import logging

import httpx

from snap_dashboard.agents.base import BaseAgent
from snap_dashboard.auth import get_user_config
from snap_dashboard.db.models import UserConfig, VersionBumpPR
from snap_dashboard.db.session import get_session

logger = logging.getLogger(__name__)

_GH_API = "https://api.github.com"

# Statuses that mean "still waiting for something"
_IN_FLIGHT = {"open", "ci_pending", "ci_passed", "yarf_running"}


def _gh_headers(token: str) -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


class PRMonitorAgent(BaseAgent):
    """Polls all in-flight VersionBumpPRs across all users and advances their status.

    - open → ci_pending: when a CI run is found for the PR
    - ci_pending → ci_passed/ci_failed: when the CI run concludes
    - ci_passed → yarf_running: triggers YARF test automatically
    - yarf_running → yarf_passed/yarf_failed: via existing TestRun sync
    - yarf_passed/yarf_failed → agent_approved/agent_rejected/needs_review:
        triggers ScreenshotReviewerAgent
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

        owner, _, repo = _extract_slug(pkg_repo).partition("/") if pkg_repo else ("", "/", "")
        if not repo:
            return False

        if status == "open":
            return self._check_ci_start(pr, owner, repo, token)
        if status == "ci_pending":
            return self._check_ci_complete(pr, owner, repo, token)
        if status == "ci_passed":
            return self._trigger_yarf(pr, uc)
        if status == "yarf_running":
            return self._check_yarf(pr)
        return False

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

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


def _extract_slug(repo: str) -> str:
    repo = repo.strip()
    if repo.startswith("https://github.com/"):
        repo = repo[len("https://github.com/"):]
    elif repo.startswith("http://github.com/"):
        repo = repo[len("http://github.com/"):]
    return repo.rstrip("/").rstrip(".git")
