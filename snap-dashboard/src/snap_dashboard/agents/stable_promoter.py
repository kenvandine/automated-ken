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
    """Promote every architecture's approved candidate test run to stable together.

    Mirrors what ``snapcraft promote`` does for a multi-arch build: a
    version bump can carry one ``TestRun`` per architecture (see
    ``agents/pr_monitor.py:_trigger_yarf``), and this agent only runs once
    every one of them has been approved (see
    ``agents/screenshot_reviewer.py``) — so the whole release set for this
    version goes to stable as one unit rather than each architecture
    trickling out on its own schedule.
    """

    agent_type = "stable_promoter"

    def __init__(
        self,
        version_bump_pr_id: int,
        test_run_ids: list[int],
        user_id: int | None = None,
    ) -> None:
        super().__init__(user_id=user_id)
        self.version_bump_pr_id = version_bump_pr_id
        self.test_run_ids = test_run_ids

    def _run(self) -> str:
        uc = get_user_config(self.user_id) if self.user_id else None
        macaroon = (getattr(uc, "snapcraft_macaroon", "") or "") if uc else ""

        with get_session() as session:
            bump = session.query(VersionBumpPR).get(self.version_bump_pr_id)
            runs = [r for r in (session.query(TestRun).get(rid) for rid in self.test_run_ids) if r]
            if not bump or not runs:
                return "promotion skipped: missing version bump PR or test run(s)"
            snap_name = runs[0].snap_name
            infos = [
                {
                    "id": r.id,
                    "arch": r.architecture or "amd64",
                    "revision": r.revision,
                    "version": r.version or "",
                    "pr_number": r.pr_number,
                    "repo": r.repo or ((uc.testing_repo if uc else "") or ""),
                }
                for r in runs
            ]

        missing = [i["arch"] for i in infos if i["revision"] is None]
        if missing:
            _mark_promotion_failed(
                self.version_bump_pr_id,
                f"No candidate revision available for: {', '.join(missing)}.",
            )
            return f"{snap_name}: promotion failed (missing revision for {', '.join(missing)})"

        arch_list = ", ".join(i["arch"] for i in infos)
        self._report(f"Promoting {snap_name} ({arch_list}) to stable…", snap_name)

        promoted_infos = []
        failed_arches = []
        for info in infos:
            ok, output = promote_snap(snap_name, info["revision"], "stable", store_credentials=macaroon)
            if ok:
                promoted_infos.append(info)
            else:
                failed_arches.append(f"{info['arch']} rev {info['revision']}: {output[:200]}")

        if failed_arches:
            # Mark whichever architectures did succeed as promoted so a
            # retry doesn't try to re-release them, and surface exactly
            # which ones still need attention.
            with get_session() as session:
                for info in promoted_infos:
                    run = session.query(TestRun).get(info["id"])
                    if run:
                        run.status = "promoted"
                        run.promoted = True
                        run.promoted_at = datetime.now(timezone.utc)
            _mark_promotion_failed(
                self.version_bump_pr_id,
                "Promotion failed for: " + "; ".join(failed_arches),
            )
            return f"{snap_name}: promotion failed for {len(failed_arches)}/{len(infos)} arch(es)"

        baseline_count = 0
        for info in infos:
            baseline_count += persist_stable_baseline_for_run(
                info["id"], info["repo"], (uc.github_token if uc else "") or "",
            )

        with get_session() as session:
            bump = session.query(VersionBumpPR).get(self.version_bump_pr_id)
            for info in infos:
                run = session.query(TestRun).get(info["id"])
                if run:
                    run.status = "promoted"
                    run.promoted = True
                    run.promoted_at = datetime.now(timezone.utc)
            if bump:
                bump.status = "stable_promoted"
                extra = f" Promoted to stable automatically ({arch_list})."
                if baseline_count:
                    extra += f" Stored {baseline_count} baseline screenshot(s)."
                if bump.agent_reasoning:
                    bump.agent_reasoning = f"{bump.agent_reasoning}{extra}"
                else:
                    bump.agent_reasoning = extra.strip()

        # All these architecture-specific runs were dispatched to remote
        # runners (not GitHub Actions), so none of them has an associated
        # test PR in practice — pr_number stays None. Kept for parity with
        # the older GH-Actions dispatch path, in case that ever resumes.
        primary = infos[0]
        if primary["repo"] and primary["pr_number"]:
            close_test_pr(
                primary["repo"],
                primary["pr_number"],
                snap_name,
                primary["version"],
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

        return f"{snap_name}: promoted to stable ({arch_list})"


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
