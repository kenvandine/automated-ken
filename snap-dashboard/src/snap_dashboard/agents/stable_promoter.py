"""Stable promotion agent for agent-approved candidate test runs."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from snap_dashboard.agents.base import BaseAgent
from snap_dashboard.auth import get_user_config
from snap_dashboard.db.models import TestRun, VersionBumpPR
from snap_dashboard.db.session import get_session
from snap_dashboard.testing.baselines import persist_stable_baseline_for_run
from snap_dashboard.testing.promoter import close_test_pr, merge_packaging_pr, promote_snap

logger = logging.getLogger(__name__)


class StablePromoterAgent(BaseAgent):
    """Promote an approved candidate test run to stable and persist its baseline."""

    agent_type = "stable_promoter"

    def __init__(
        self,
        version_bump_pr_id: int,
        test_run_id: int,
        user_id: int | None = None,
    ) -> None:
        super().__init__(user_id=user_id)
        self.version_bump_pr_id = version_bump_pr_id
        self.test_run_id = test_run_id

    def _run(self) -> str:
        uc = get_user_config(self.user_id) if self.user_id else None

        with get_session() as session:
            bump = session.query(VersionBumpPR).get(self.version_bump_pr_id)
            run = session.query(TestRun).get(self.test_run_id)
            if not bump or not run:
                return "promotion skipped: missing version bump PR or test run"
            snap_name = run.snap_name
            revision = run.revision
            version = run.version or ""
            pr_number = run.pr_number

        if revision is None:
            _mark_promotion_failed(self.version_bump_pr_id, "No candidate revision available for stable promotion.")
            return f"{snap_name}: promotion failed (missing revision)"

        self._report(f"Promoting {snap_name} rev {revision} to stable…", snap_name)
        ok, output = promote_snap(snap_name, revision, "stable")
        if not ok:
            _mark_promotion_failed(self.version_bump_pr_id, output[:500] or "snapcraft release failed")
            return f"{snap_name}: promotion failed"

        baseline_count = persist_stable_baseline_for_run(
            self.test_run_id,
            (uc.testing_repo if uc else "") or "",
            (uc.github_token if uc else "") or "",
        )

        with get_session() as session:
            bump = session.query(VersionBumpPR).get(self.version_bump_pr_id)
            run = session.query(TestRun).get(self.test_run_id)
            if run:
                run.status = "promoted"
                run.promoted = True
                run.promoted_at = datetime.now(timezone.utc)
            if bump:
                bump.status = "stable_promoted"
                extra = " Promoted to stable automatically."
                if baseline_count:
                    extra += f" Stored {baseline_count} baseline screenshot(s)."
                if bump.agent_reasoning:
                    bump.agent_reasoning = f"{bump.agent_reasoning}{extra}"
                else:
                    bump.agent_reasoning = extra.strip()

        if uc and uc.testing_repo and pr_number:
            close_test_pr(
                uc.testing_repo,
                pr_number,
                snap_name,
                version,
                uc.github_token,
            )

        if uc and uc.auto_merge:
            merged = _auto_merge_packaging_pr(self.version_bump_pr_id, uc.github_token or "")
            if merged:
                with get_session() as session:
                    bump = session.query(VersionBumpPR).get(self.version_bump_pr_id)
                    if bump:
                        bump.status = "merged"
                        bump.merged_at = datetime.now(timezone.utc)
                        bump.agent_reasoning = (
                            f"{bump.agent_reasoning or ''} Packaging PR auto-merged."
                        ).strip()

        return f"{snap_name}: promoted to stable"


def _mark_promotion_failed(version_bump_pr_id: int, message: str) -> None:
    with get_session() as session:
        bump = session.query(VersionBumpPR).get(version_bump_pr_id)
        if bump:
            bump.status = "promotion_failed"
            bump.agent_reasoning = message


def _auto_merge_packaging_pr(version_bump_pr_id: int, token: str) -> bool:
    if not token:
        return False
    with get_session() as session:
        bump = session.query(VersionBumpPR).get(version_bump_pr_id)
        if not bump or not bump.packaging_repo or not bump.bot_pr_number:
            return False
        return merge_packaging_pr(bump.packaging_repo, bump.bot_pr_number, token)
