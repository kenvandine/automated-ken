"""Screenshot reviewer agent — LLM vision comparison of before/after screenshots."""

from __future__ import annotations

import logging

import httpx

from snap_dashboard.agents.base import BaseAgent
from snap_dashboard.auth import get_user_config
from snap_dashboard.db.models import ScreenshotComparison, TestRun, VersionBumpPR
from snap_dashboard.db.session import get_session
from snap_dashboard.github.pr_viewer import get_pr_screenshot_urls

logger = logging.getLogger(__name__)

_GH_API = "https://api.github.com"


class ScreenshotReviewerAgent(BaseAgent):
    """Compares YARF screenshots for a version bump using an LLM vision model.

    Decision outcomes:
      - approve    → sets VersionBumpPR.status = agent_approved
      - reject     → sets VersionBumpPR.status = agent_rejected
      - needs_review → sets VersionBumpPR.status = needs_review
    """

    agent_type = "screenshot_reviewer"

    def __init__(
        self,
        version_bump_pr_id: int,
        test_run_id: int | None = None,
        user_id: int | None = None,
    ) -> None:
        super().__init__(user_id=user_id)
        self.version_bump_pr_id = version_bump_pr_id
        self.test_run_id = test_run_id

    def _run(self) -> str:
        uc = get_user_config(self.user_id) if self.user_id else None
        token = (uc.github_token if uc else "") or ""

        with get_session() as session:
            bump = session.query(VersionBumpPR).get(self.version_bump_pr_id)
            if not bump:
                return f"no VersionBumpPR with id={self.version_bump_pr_id}"
            snap_id = bump.snap_id
            snap_name = bump.snap.name if bump.snap else str(snap_id)
            old_version = bump.old_version or ""
            new_version = bump.new_version or ""
            test_run_id = self.test_run_id or bump.test_run_id
            yarf_status = bump.status  # yarf_passed or yarf_failed

        # Load test run details
        pr_body = ""
        pr_branch = ""
        testing_repo = (uc.testing_repo if uc else "") or ""
        with get_session() as session:
            if test_run_id:
                run = session.query(TestRun).get(test_run_id)
                if run:
                    pr_body = run.pr_body or ""
                    pr_branch = f"test-results/{snap_name}/{run.gh_run_id}" if run.gh_run_id else ""

        self._report(f"Fetching YARF screenshots for {snap_name}…", snap_name)
        new_screenshots: list[str] = []
        if pr_body and testing_repo and token:
            from snap_dashboard.github.pr_viewer import get_test_prs, parse_pr_metadata
            try:
                with get_session() as session:
                    run = session.query(TestRun).get(test_run_id) if test_run_id else None
                    if run and run.pr_number:
                        new_screenshots = get_pr_screenshot_urls(
                            testing_repo=testing_repo,
                            pr_data={"number": run.pr_number, "body": pr_body},
                            branch=pr_branch,
                            token=token,
                        )
            except Exception as exc:
                logger.warning("screenshot_reviewer: failed fetching new screenshots: %s", exc)

        # Fetch baseline screenshots from the most recent passing TestRun for stable
        baseline_screenshots: list[str] = []
        if testing_repo and token:
            baseline_screenshots = self._get_baseline_screenshots(
                snap_name, testing_repo, token
            )

        # Decide: use LLM if available, else heuristic
        lemonade = self._get_lemonade(uc)
        decision_dict = None

        if lemonade and new_screenshots and baseline_screenshots:
            self._report(f"⚡ Lemonade AI comparing screenshots — {snap_name} {old_version}→{new_version}", snap_name)
            decision_dict = self._llm_compare(
                lemonade,
                baseline_screenshots[0],
                new_screenshots[0],
                snap_name,
                old_version,
                new_version,
                token,
            )

        if decision_dict is None:
            decision_dict = self._heuristic_decision(yarf_status, new_screenshots)

        decision = decision_dict["decision"]
        confidence = decision_dict["confidence"]
        reasoning = decision_dict["reasoning"]

        # Map YARF failure to reject regardless
        if yarf_status == "yarf_failed" and decision == "approve":
            decision = "reject"
            reasoning = f"YARF tests failed. {reasoning}"

        # Persist comparison record
        with get_session() as session:
            comp = ScreenshotComparison(
                version_bump_pr_id=self.version_bump_pr_id,
                test_run_id=test_run_id,
                baseline_url=baseline_screenshots[0] if baseline_screenshots else None,
                new_url=new_screenshots[0] if new_screenshots else None,
                decision=decision,
                confidence=confidence,
                reasoning=reasoning,
                llm_prompt="vision_compare" if lemonade and decision_dict else None,
            )
            session.add(comp)

        # Update VersionBumpPR
        final_status = _decision_to_status(decision)
        with get_session() as session:
            bump = session.query(VersionBumpPR).get(self.version_bump_pr_id)
            if bump:
                bump.agent_decision = decision
                bump.agent_confidence = confidence
                bump.agent_reasoning = reasoning
                bump.status = final_status

        logger.info(
            "screenshot_reviewer: %s → %s (confidence=%.2f)",
            snap_name, decision, confidence,
        )
        return f"{snap_name}: {decision} (confidence={confidence:.0%})"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _llm_compare(
        self,
        lemonade,
        baseline_url: str,
        new_url: str,
        snap_name: str,
        old_version: str,
        new_version: str,
        token: str,
    ) -> dict | None:
        baseline_bytes = _download_image(baseline_url, token)
        new_bytes = _download_image(new_url, token)
        if not baseline_bytes or not new_bytes:
            return None
        return lemonade.vision_compare(
            baseline_bytes=baseline_bytes,
            new_bytes=new_bytes,
            snap_name=snap_name,
            old_version=old_version,
            new_version=new_version,
        )

    def _heuristic_decision(self, yarf_status: str, screenshots: list[str]) -> dict:
        """Rule-based fallback when LLM is unavailable."""
        if yarf_status == "yarf_passed" and screenshots:
            return {
                "decision": "needs_review",
                "confidence": 0.5,
                "reasoning": (
                    "YARF tests passed and screenshots are present, but no vision model "
                    "is available for automated comparison. Manual review required."
                ),
            }
        if yarf_status == "yarf_passed":
            return {
                "decision": "needs_review",
                "confidence": 0.4,
                "reasoning": (
                    "YARF tests passed but no screenshots were captured. "
                    "Manual review required."
                ),
            }
        return {
            "decision": "reject",
            "confidence": 0.9,
            "reasoning": "YARF tests failed.",
        }

    def _get_baseline_screenshots(
        self, snap_name: str, testing_repo: str, token: str
    ) -> list[str]:
        """Return screenshot URLs from the most recent passing stable test run."""
        with get_session() as session:
            from snap_dashboard.db.models import TestRun
            run = (
                session.query(TestRun)
                .filter_by(snap_name=snap_name, status="passed", from_channel="stable")
                .order_by(TestRun.finished_at.desc())
                .first()
            )
            if not run or not run.pr_body or not run.pr_number:
                return []
            pr_body = run.pr_body
            pr_number = run.pr_number
            gh_run_id = run.gh_run_id or ""

        branch = f"test-results/{snap_name}/{gh_run_id}" if gh_run_id else ""
        try:
            return get_pr_screenshot_urls(
                testing_repo=testing_repo,
                pr_data={"number": pr_number, "body": pr_body},
                branch=branch,
                token=token,
            )
        except Exception:
            return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decision_to_status(decision: str) -> str:
    return {
        "approve": "agent_approved",
        "reject": "agent_rejected",
        "needs_review": "needs_review",
    }.get(decision, "needs_review")


def _download_image(url: str, token: str = "") -> bytes | None:
    """Download a PNG from a GitHub raw URL."""
    headers: dict[str, str] = {}
    if token and "github.com" in url:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
        if resp.status_code == 200:
            return resp.content
    except Exception as exc:
        logger.debug("_download_image failed for %s: %s", url, exc)
    return None
