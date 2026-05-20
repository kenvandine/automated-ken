"""Screenshot reviewer agent — LLM vision comparison of before/after screenshots."""

from __future__ import annotations

import logging

from snap_dashboard.agents.base import BaseAgent
from snap_dashboard.auth import get_user_config
from snap_dashboard.db.models import ScreenshotComparison, TestRun, VersionBumpPR
from snap_dashboard.db.session import get_session
from snap_dashboard.testing.baselines import (
    ScreenshotAsset,
    get_or_build_stable_baseline_assets,
    load_test_run_screenshots,
    pair_screenshots,
)

logger = logging.getLogger(__name__)


class ScreenshotReviewerAgent(BaseAgent):
    """Compares YARF screenshots for a version bump using an LLM vision model."""

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
        testing_repo = (uc.testing_repo if uc else "") or ""

        with get_session() as session:
            bump = session.query(VersionBumpPR).get(self.version_bump_pr_id)
            if not bump:
                return f"no VersionBumpPR with id={self.version_bump_pr_id}"
            snap_id = bump.snap_id
            snap_name = bump.snap.name if bump.snap else str(snap_id)
            old_version = bump.old_version or ""
            new_version = bump.new_version or ""
            test_run_id = self.test_run_id or bump.test_run_id
            yarf_status = bump.status
            run = session.query(TestRun).get(test_run_id) if test_run_id else None
            architecture = (run.architecture if run and run.architecture else "amd64")
            from_channel = (run.from_channel if run else "")
            revision = run.revision if run else None
            pr_number = run.pr_number if run else None

        self._report(f"Fetching YARF screenshots for {snap_name}…", snap_name)
        new_screenshots = load_test_run_screenshots(testing_repo, pr_number, token)
        baseline_screenshots: list[ScreenshotAsset] = []
        if testing_repo:
            baseline_screenshots = get_or_build_stable_baseline_assets(
                self.user_id,
                snap_name,
                architecture,
                testing_repo,
                token,
            )
        comparison_pairs = pair_screenshots(baseline_screenshots, new_screenshots)

        lemonade = self._get_lemonade(uc)
        decision_dict = None

        if lemonade and comparison_pairs:
            self._report(
                f"⚡ Lemonade AI comparing screenshots — {snap_name} {old_version}→{new_version}",
                snap_name,
            )
            decisions = []
            for baseline_asset, new_asset in comparison_pairs:
                result = self._llm_compare(
                    lemonade,
                    baseline_asset,
                    new_asset,
                    snap_name,
                    old_version,
                    new_version,
                )
                if result:
                    decisions.append(result)
            if decisions:
                decision_dict = _aggregate_decisions(decisions)

        if decision_dict is None:
            decision_dict = self._heuristic_decision(yarf_status, new_screenshots)

        decision = decision_dict["decision"]
        confidence = decision_dict["confidence"]
        reasoning = decision_dict["reasoning"]

        if yarf_status == "yarf_failed" and decision == "approve":
            decision = "reject"
            reasoning = f"YARF tests failed. {reasoning}"

        representative_baseline = comparison_pairs[0][0] if comparison_pairs else (
            baseline_screenshots[0] if baseline_screenshots else None
        )
        representative_new = comparison_pairs[0][1] if comparison_pairs else (
            new_screenshots[0] if new_screenshots else None
        )

        with get_session() as session:
            comp = ScreenshotComparison(
                version_bump_pr_id=self.version_bump_pr_id,
                test_run_id=test_run_id,
                baseline_url=representative_baseline.image_url if representative_baseline else None,
                baseline_image_b64=representative_baseline.image_b64 if representative_baseline else None,
                new_url=representative_new.image_url if representative_new else None,
                new_image_b64=representative_new.image_b64 if representative_new else None,
                decision=decision,
                confidence=confidence,
                reasoning=reasoning,
                llm_prompt="vision_compare" if lemonade and comparison_pairs else None,
            )
            session.add(comp)

        final_status = _decision_to_status(decision)
        should_auto_promote = (
            decision == "approve"
            and yarf_status == "yarf_passed"
            and bool(getattr(uc, "auto_promote", False))
            and confidence >= float(getattr(uc, "auto_promote_confidence", 0.85) or 0.85)
            and from_channel == "candidate"
            and revision is not None
            and test_run_id is not None
        )
        if should_auto_promote:
            final_status = "promoting"

        with get_session() as session:
            bump = session.query(VersionBumpPR).get(self.version_bump_pr_id)
            if bump:
                bump.agent_decision = decision
                bump.agent_confidence = confidence
                bump.agent_reasoning = reasoning
                bump.status = final_status

        if should_auto_promote and test_run_id is not None:
            from snap_dashboard.agents.runner import get_runner
            from snap_dashboard.agents.stable_promoter import StablePromoterAgent

            get_runner().submit(
                StablePromoterAgent(
                    version_bump_pr_id=self.version_bump_pr_id,
                    test_run_id=test_run_id,
                    user_id=self.user_id,
                )
            )

        logger.info(
            "screenshot_reviewer: %s → %s (confidence=%.2f)",
            snap_name,
            decision,
            confidence,
        )
        return f"{snap_name}: {decision} (confidence={confidence:.0%})"

    def _llm_compare(
        self,
        lemonade,
        baseline_asset: ScreenshotAsset,
        new_asset: ScreenshotAsset,
        snap_name: str,
        old_version: str,
        new_version: str,
    ) -> dict | None:
        return lemonade.vision_compare(
            baseline_bytes=baseline_asset.image_bytes,
            new_bytes=new_asset.image_bytes,
            snap_name=snap_name,
            old_version=old_version,
            new_version=new_version,
        )

    def _heuristic_decision(self, yarf_status: str, screenshots: list[ScreenshotAsset]) -> dict:
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


def _decision_to_status(decision: str) -> str:
    return {
        "approve": "agent_approved",
        "reject": "agent_rejected",
        "needs_review": "needs_review",
    }.get(decision, "needs_review")


def _aggregate_decisions(decisions: list[dict]) -> dict:
    """Collapse multiple screenshot comparisons into one conservative decision."""
    rejects = [d for d in decisions if d["decision"] == "reject"]
    if rejects:
        confidence = max(float(d["confidence"]) for d in rejects)
        reasoning = " ".join(d["reasoning"] for d in rejects if d.get("reasoning"))
        return {
            "decision": "reject",
            "confidence": confidence,
            "reasoning": reasoning or "One or more screenshots showed a regression.",
        }

    reviews = [d for d in decisions if d["decision"] == "needs_review"]
    if reviews:
        confidence = min(float(d["confidence"]) for d in reviews)
        reasoning = " ".join(d["reasoning"] for d in reviews if d.get("reasoning"))
        return {
            "decision": "needs_review",
            "confidence": confidence,
            "reasoning": reasoning or "Screenshot comparison needs manual review.",
        }

    confidence = min(float(d["confidence"]) for d in decisions)
    reasoning = " ".join(d["reasoning"] for d in decisions if d.get("reasoning"))
    return {
        "decision": "approve",
        "confidence": confidence,
        "reasoning": reasoning or "Compared screenshots are consistent with the stored stable baseline.",
    }
