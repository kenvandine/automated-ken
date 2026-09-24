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
            testing_repo = (run.repo if run and run.repo else None) or testing_repo

        self._report(f"Fetching YARF screenshots for {snap_name}…", snap_name)
        new_screenshots = load_test_run_screenshots(
            testing_repo, pr_number, token, test_run_id=test_run_id
        )
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

        lemonade = self._get_lemonade(uc, task="vision")
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

        should_auto_promote = False
        sibling_ids: list[int] = []
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
            session.flush()

            # Every architecture of this version bump gets its own TestRun
            # (see agents/pr_monitor.py:_trigger_yarf) and thus its own
            # ScreenshotReviewerAgent — only finalize the PR / consider
            # promotion once every one of them has recorded a decision, so
            # e.g. an arm64 regression can still block a passing amd64 run
            # from being promoted alone.
            sibling_runs = (
                session.query(TestRun).filter_by(version_bump_pr_id=self.version_bump_pr_id).all()
            )
            sibling_ids = [r.id for r in sibling_runs] if sibling_runs else (
                [test_run_id] if test_run_id is not None else []
            )
            runs_by_id = {r.id: r for r in sibling_runs}

            latest_by_run: dict[int, ScreenshotComparison] = {}
            if sibling_ids:
                for row in (
                    session.query(ScreenshotComparison)
                    .filter(ScreenshotComparison.test_run_id.in_(sibling_ids))
                    .order_by(ScreenshotComparison.id.asc())
                    .all()
                ):
                    latest_by_run[row.test_run_id] = row  # keep the highest id per run

            pending = [rid for rid in sibling_ids if rid not in latest_by_run]
            if pending:
                logger.info(
                    "screenshot_reviewer: %s test_run=%s recorded %s; waiting on %d more architecture(s)",
                    snap_name, test_run_id, decision, len(pending),
                )
                return f"{snap_name}: {decision} (confidence={confidence:.0%}, waiting on {len(pending)} more arch(es))"

            decisions = [
                {
                    "decision": latest_by_run[rid].decision,
                    "confidence": latest_by_run[rid].confidence or 0.0,
                    "reasoning": latest_by_run[rid].reasoning or "",
                }
                for rid in sibling_ids
            ]
            aggregate = _aggregate_decisions(decisions)
            agg_decision = aggregate["decision"]
            agg_confidence = aggregate["confidence"]
            agg_reasoning = aggregate["reasoning"]

            final_status = _decision_to_status(agg_decision)
            all_candidate = all(
                (runs_by_id[rid].from_channel if rid in runs_by_id else from_channel) == "candidate"
                for rid in sibling_ids
            )
            all_have_revision = all(
                (runs_by_id[rid].revision if rid in runs_by_id else revision) is not None
                for rid in sibling_ids
            )
            should_auto_promote = (
                agg_decision == "approve"
                and yarf_status == "yarf_passed"
                and bool(getattr(uc, "auto_promote", False))
                and agg_confidence >= float(getattr(uc, "auto_promote_confidence", 0.85) or 0.85)
                and all_candidate
                and all_have_revision
                and bool(sibling_ids)
            )
            if should_auto_promote:
                final_status = "promoting"

            bump = session.query(VersionBumpPR).get(self.version_bump_pr_id)
            if bump:
                bump.agent_decision = agg_decision
                bump.agent_confidence = agg_confidence
                bump.agent_reasoning = agg_reasoning
                bump.status = final_status

        if should_auto_promote:
            from snap_dashboard.agents.runner import get_runner
            from snap_dashboard.agents.stable_promoter import StablePromoterAgent

            get_runner().submit(
                StablePromoterAgent(
                    version_bump_pr_id=self.version_bump_pr_id,
                    test_run_ids=sibling_ids,
                    user_id=self.user_id,
                )
            )

        logger.info(
            "screenshot_reviewer: %s → %s (confidence=%.2f, %d arch(es))",
            snap_name,
            agg_decision,
            agg_confidence,
            len(sibling_ids),
        )
        return f"{snap_name}: {agg_decision} (confidence={agg_confidence:.0%}, {len(sibling_ids)} arch(es))"

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
