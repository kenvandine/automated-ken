"""Auto-promote candidate test runs after visual approval."""

from __future__ import annotations

import logging

from snap_dashboard.agents.base import BaseAgent
from snap_dashboard.agents.screenshot_reviewer import _aggregate_decisions
from snap_dashboard.auth import get_user_config
from snap_dashboard.db.models import TestRun
from snap_dashboard.db.session import get_session
from snap_dashboard.testing.baselines import (
    get_or_build_stable_baseline_assets,
    load_test_run_screenshots,
    pair_screenshots,
)
from snap_dashboard.testing.promoter import close_test_pr

logger = logging.getLogger(__name__)


class TestRunAutoPromoterAgent(BaseAgent):
    """Review a candidate test run's screenshots; promote its release set once all are approved.

    The review result is always recorded on the run. With auto-promote on,
    an approved run only triggers promotion when every testable
    architecture of the same candidate version is approved as well, and
    then the whole set is released together (see testing/release_set.py).
    """

    agent_type = "test_run_auto_promoter"

    def __init__(self, test_run_id: int, user_id: int | None = None) -> None:
        super().__init__(user_id=user_id)
        self.test_run_id = test_run_id

    def _run(self) -> str:
        uc = get_user_config(self.user_id) if self.user_id else None
        if not uc:
            return "no user config"

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
            effective_repo = run.repo or uc.testing_repo

        if revision is None:
            _set_run_note(self.test_run_id, "Review skipped: candidate revision is missing.")
            return f"{snap_name}: missing revision"

        baseline_assets = get_or_build_stable_baseline_assets(
            self.user_id,
            snap_name,
            architecture,
            effective_repo,
            uc.github_token,
        )
        new_assets = load_test_run_screenshots(
            effective_repo, pr_number, uc.github_token, test_run_id=self.test_run_id
        )
        if not new_assets:
            _set_run_note(
                self.test_run_id,
                "Review skipped: no screenshots are available yet for this run.",
            )
            return f"{snap_name}: no comparable screenshots"

        pairs = pair_screenshots(baseline_assets, new_assets)

        lemonade = self._get_lemonade(uc, task="vision")
        if not lemonade:
            _set_run_note(
                self.test_run_id,
                "Review skipped: no vision model is available for screenshot comparison.",
            )
            return f"{snap_name}: no vision model"

        decisions = []
        if pairs:
            # Normal path: a known-good stable screenshot exists, so the
            # model does a real before/after comparison.
            self._report(f"Reviewing candidate screenshots for {snap_name}…", snap_name)
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
        else:
            # No baseline yet (e.g. the very first tested version of this
            # snap) — there's nothing to diff against, but the model can
            # still judge each screenshot on its own (did a real
            # application window launch, vs. a blank screen/crash/error).
            # vision_inspect() caps confidence low for exactly this reason.
            self._report(
                f"No baseline yet for {snap_name} — inspecting screenshot(s) alone…", snap_name
            )
            for new_asset in new_assets:
                result = lemonade.vision_inspect(
                    image_bytes=new_asset.image_bytes,
                    snap_name=snap_name,
                    version=version,
                )
                if result:
                    decisions.append(result)

        if not decisions:
            _set_run_note(
                self.test_run_id,
                "Review skipped: screenshot comparison did not return a usable decision.",
            )
            return f"{snap_name}: comparison unavailable"

        decision = _aggregate_decisions(decisions)
        if not pairs:
            decision["reasoning"] = (
                "No stable baseline was available for comparison, so this only "
                "confirms an application window appears to have launched "
                f"(confidence intentionally kept low): {decision['reasoning']}"
            )
        threshold = float(getattr(uc, "auto_promote_confidence", 0.85) or 0.85)

        # The review itself — and its result — always happens and is always
        # recorded/displayed, whether or not auto-promote is turned on;
        # only the actual promotion action below is gated on that setting.
        with get_session() as session:
            run = session.query(TestRun).get(self.test_run_id)
            if run:
                run.review_decision = decision["decision"]
                run.review_confidence = decision["confidence"]
                run.review_reasoning = decision["reasoning"]

        if decision["decision"] != "approve" or decision["confidence"] < threshold:
            _set_run_note(self.test_run_id, f"Review complete: {decision['reasoning']}")
            return f"{snap_name}: requires manual review"

        if not getattr(uc, "auto_promote", False):
            _set_run_note(
                self.test_run_id,
                f"Reviewed and approved (confidence {decision['confidence']:.2f}): "
                f"{decision['reasoning']} Auto-promote is off — promote manually when ready.",
            )
            return f"{snap_name}: reviewed and approved, awaiting manual promotion"

        # Approved. Only promote once every architecture of this candidate
        # version is approved too, then release them all together — see
        # testing/release_set.py for why.
        _set_run_note(
            self.test_run_id,
            f"Reviewed and approved (confidence {decision['confidence']:.2f}): {decision['reasoning']}",
        )
        from snap_dashboard.testing.release_set import (
            READY,
            candidate_release_set,
            describe,
            member_state,
            promote_release_set,
        )

        with get_session() as session:
            members = candidate_release_set(session, self.user_id, snap_name, version)
            states = [(m, member_state(m, auto_threshold=threshold)) for m in members]
            ready_ids = [m["run"].id for m, st in states if st == READY]
            waiting = [describe(m, st) for m, st in states if st not in (READY, "promoted")]

        if waiting:
            _set_run_note(
                self.test_run_id,
                f"Reviewed and approved (confidence {decision['confidence']:.2f}): "
                f"{decision['reasoning']} Waiting on the rest of the release set before "
                f"promoting: {', '.join(waiting)}.",
            )
            return f"{snap_name}: approved, waiting on {', '.join(waiting)}"
        if not ready_ids:
            return f"{snap_name}: nothing left to promote"

        arch_list = ", ".join(m["architecture"] for m, st in states if st == READY)
        self._report(f"Promoting {snap_name} {version} ({arch_list}) to stable…", snap_name)
        promoted, failures = promote_release_set(self.user_id, snap_name, version, ready_ids, uc)
        if failures:
            return f"{snap_name}: promoted {', '.join(promoted) or 'nothing'}; failed: {'; '.join(failures)}"

        if pr_number:
            close_test_pr(effective_repo, pr_number, snap_name, version, uc.github_token)

        return f"{snap_name}: promoted {version} to stable ({', '.join(promoted)})"


def _set_run_note(test_run_id: int, message: str) -> None:
    with get_session() as session:
        run = session.query(TestRun).get(test_run_id)
        if run:
            run.status = "passed"
            run.error_msg = message[:500]
            # Only record this as the review reasoning when a real decision
            # wasn't already reached (a genuine approve/reject/needs_review
            # from _aggregate_decisions() takes priority — see the call
            # sites above that set run.review_decision directly).
            if not run.review_decision:
                run.review_reasoning = message[:2000]
