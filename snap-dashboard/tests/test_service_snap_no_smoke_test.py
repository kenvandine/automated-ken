"""Tests for Snap.is_service — snaps with no UI (daemons/services) skip the
smoke test entirely (see automated_ken_runner.runner._run_job) and are
auto-approved by the review agents instead of getting stuck waiting for
screenshots that will never exist.
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from snap_dashboard.agents import screenshot_reviewer as reviewer_mod
from snap_dashboard.agents import test_run_auto_promoter as promoter_mod
from snap_dashboard.db.models import Base, Snap, TestRun


class ScreenshotReviewerHeuristicServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = reviewer_mod.ScreenshotReviewerAgent(version_bump_pr_id=1)

    def test_service_snap_auto_approved_when_yarf_passed(self) -> None:
        decision = self.agent._heuristic_decision("yarf_passed", [], is_service=True)
        self.assertEqual(decision["decision"], "approve")
        self.assertEqual(decision["confidence"], 1.0)
        self.assertIn("service", decision["reasoning"].lower())

    def test_service_snap_rejected_when_smoke_test_failed(self) -> None:
        decision = self.agent._heuristic_decision("yarf_failed", [], is_service=True)
        self.assertEqual(decision["decision"], "reject")

    def test_non_service_snap_still_needs_manual_review(self) -> None:
        decision = self.agent._heuristic_decision("yarf_passed", [], is_service=False)
        self.assertEqual(decision["decision"], "needs_review")


class TestRunAutoPromoterServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        self.session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)

        @contextmanager
        def _fake_get_session():
            session = self.session_local()
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        self._fake_get_session = _fake_get_session

    def test_service_snap_with_no_screenshots_is_auto_approved(self) -> None:
        import unittest.mock as mock

        with mock.patch.object(promoter_mod, "get_session", self._fake_get_session):
            with self.session_local() as session:
                snap = Snap(user_id=1, name="my-daemon", is_service=True)
                session.add(snap)
                run = TestRun(
                    user_id=1,
                    snap_name="my-daemon",
                    architecture="amd64",
                    from_channel="candidate",
                    version="1.2",
                    revision=3,
                    status="passed",
                    triggered_by="manual",
                )
                session.add(run)
                session.commit()
                run_id = run.id

            with mock.patch.object(
                promoter_mod,
                "get_user_config",
                lambda uid: SimpleNamespace(
                    github_token="tok", testing_repo="owner/repo",
                    auto_promote_confidence=0.85, auto_promote=False,
                    snapcraft_macaroon="",
                ),
            ):
                agent = promoter_mod.TestRunAutoPromoterAgent(test_run_id=run_id, user_id=1)
                summary = agent._run()

            self.assertIn("awaiting manual promotion", summary)
            with self.session_local() as session:
                updated = session.query(TestRun).get(run_id)
                self.assertEqual(updated.review_decision, "approve")
                self.assertEqual(updated.review_confidence, 1.0)
                self.assertIn("service", (updated.review_reasoning or "").lower())


if __name__ == "__main__":
    unittest.main()
