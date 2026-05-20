"""Auto-promote candidate test runs after visual approval."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from snap_dashboard.agents.base import BaseAgent
from snap_dashboard.agents.screenshot_reviewer import _aggregate_decisions
from snap_dashboard.auth import get_user_config
from snap_dashboard.db.models import TestRun, VersionBumpPR
from snap_dashboard.db.session import get_session
from snap_dashboard.testing.baselines import (
    get_or_build_stable_baseline_assets,
    load_test_run_screenshots,
    pair_screenshots,
    persist_stable_baseline_for_run,
)
from snap_dashboard.testing.promoter import close_test_pr, merge_packaging_pr, promote_snap

logger = logging.getLogger(__name__)


class TestRunAutoPromoterAgent(BaseAgent):
    """Compare a candidate test run against the stored baseline and promote on approval."""

    agent_type = "test_run_auto_promoter"

    def __init__(self, test_run_id: int, user_id: int | None = None) -> None:
        super().__init__(user_id=user_id)
        self.test_run_id = test_run_id

    def _run(self) -> str:
        uc = get_user_config(self.user_id) if self.user_id else None
        if not uc or not getattr(uc, "auto_promote", False):
            return "auto-promote disabled"

        with get_session() as session:
            run = session.query(TestRun).get(self.test_run_id)
            if not run:
                return f"missing TestRun {self.test_run_id}"
            if run.promoted or run.status == "promoted":
                return f"{run.snap_name}: already promoted"
            if run.status not in ("passed", "reviewing") or run.from_channel != "candidate":
                return f"{run.snap_name}: not eligible for auto-promotion"

            snap_name = run.snap_name
            architecture = run.architecture or "amd64"
            revision = run.revision
            pr_number = run.pr_number
            version = run.version or ""

        if revision is None:
            _set_run_note(self.test_run_id, "Auto-promote skipped: candidate revision is missing.")
            return f"{snap_name}: missing revision"

        baseline_assets = get_or_build_stable_baseline_assets(
            self.user_id,
            snap_name,
            architecture,
            uc.testing_repo,
            uc.github_token,
        )
        new_assets = load_test_run_screenshots(uc.testing_repo, pr_number, uc.github_token)
        pairs = pair_screenshots(baseline_assets, new_assets)
        if not pairs:
            _set_run_note(
                self.test_run_id,
                "Auto-promote skipped: no stable baseline or comparable screenshots are available yet.",
            )
            return f"{snap_name}: no comparable screenshots"

        lemonade = self._get_lemonade(uc)
        if not lemonade:
            _set_run_note(
                self.test_run_id,
                "Auto-promote skipped: no vision model is available for screenshot comparison.",
            )
            return f"{snap_name}: no vision model"

        self._report(f"Reviewing candidate screenshots for {snap_name}…", snap_name)
        decisions = []
        for baseline_asset, new_asset in pairs:
            result = lemonade.vision_compare(
                baseline_bytes=baseline_asset.image_bytes,
                new_bytes=new_asset.image_bytes,
                snap_name=snap_name,
                old_version="stable",
                new_version=version,
            )
            if result:
                decisions.append(result)

        if not decisions:
            _set_run_note(
                self.test_run_id,
                "Auto-promote skipped: screenshot comparison did not return a usable decision.",
            )
            return f"{snap_name}: comparison unavailable"

        decision = _aggregate_decisions(decisions)
        threshold = float(getattr(uc, "auto_promote_confidence", 0.85) or 0.85)
        if decision["decision"] != "approve" or decision["confidence"] < threshold:
            _set_run_note(
                self.test_run_id,
                f"Auto-promote skipped: {decision['reasoning']}",
            )
            return f"{snap_name}: requires manual review"

        self._report(f"Promoting {snap_name} rev {revision} to stable…", snap_name)
        ok, output = promote_snap(snap_name, revision, "stable")
        if not ok:
            _set_run_note(self.test_run_id, f"Auto-promote failed: {output[:500]}")
            return f"{snap_name}: promotion failed"

        baseline_count = persist_stable_baseline_for_run(
            self.test_run_id,
            uc.testing_repo,
            uc.github_token,
        )
        with get_session() as session:
            run = session.query(TestRun).get(self.test_run_id)
            if run:
                run.status = "promoted"
                run.promoted = True
                run.promoted_at = datetime.now(timezone.utc)
                run.error_msg = None

            bump = session.query(VersionBumpPR).filter_by(test_run_id=self.test_run_id).first()
            if bump:
                bump.status = "stable_promoted"
                bump.agent_decision = "approve"
                bump.agent_confidence = decision["confidence"]
                bump.agent_reasoning = (
                    f"{decision['reasoning']} Promoted to stable automatically."
                )

        if pr_number:
            close_test_pr(
                uc.testing_repo,
                pr_number,
                snap_name,
                version,
                uc.github_token,
            )

        if uc.auto_merge:
            with get_session() as session:
                bump = session.query(VersionBumpPR).filter_by(test_run_id=self.test_run_id).first()
                if bump and merge_packaging_pr(bump.packaging_repo or "", bump.bot_pr_number or 0, uc.github_token or ""):
                    bump.status = "merged"
                    bump.merged_at = datetime.now(timezone.utc)
                    bump.agent_reasoning = (
                        f"{bump.agent_reasoning or ''} Packaging PR auto-merged."
                    ).strip()

        return f"{snap_name}: promoted to stable ({baseline_count} baseline screenshots stored)"


def _set_run_note(test_run_id: int, message: str) -> None:
    with get_session() as session:
        run = session.query(TestRun).get(test_run_id)
        if run:
            run.status = "passed"
            run.error_msg = message[:500]
