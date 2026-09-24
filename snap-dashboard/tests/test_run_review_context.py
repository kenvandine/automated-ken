"""Tests for the AI-review + screenshot-comparison context shared between
the run-detail and PR-detail pages, and for the new ``TestRun`` review
columns that make it possible.

Covers:
- ``TestRun.review_decision/review_confidence/review_reasoning`` columns
  exist and round-trip correctly (regression guard for the migration in
  ``db/session.py`` and the model in ``db/models.py``).
- ``_build_review_context`` in ``web/routes/testing.py`` returns paired
  baseline/new screenshots plus the review fields, and degrades gracefully
  (``has_baseline=False``, empty pairs) when no baseline exists yet — the
  exact "evince test passed, no previous screenshot" scenario reported.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from snap_dashboard.db.models import Base, TestRun
from snap_dashboard.web.routes.testing import _build_review_context


def _make_asset(name: str) -> SimpleNamespace:
    return SimpleNamespace(image_name=name, image_b64="Zm9v", image_url=None)


def test_test_run_review_columns_round_trip():
    """The new review_* columns can be written and read back."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    session = session_local()
    try:
        run = TestRun(
            user_id=1,
            snap_name="evince",
            architecture="amd64",
            from_channel="candidate",
            version="1.2.3",
            status="passed",
        )
        session.add(run)
        session.commit()

        run.review_decision = "approve"
        run.review_confidence = 0.92
        run.review_reasoning = "Screens match closely."
        session.commit()

        reloaded = session.query(TestRun).filter_by(snap_name="evince").one()
        assert reloaded.review_decision == "approve"
        assert reloaded.review_confidence == 0.92
        assert reloaded.review_reasoning == "Screens match closely."
    finally:
        session.close()


def test_build_review_context_with_baseline_pairs_screenshots():
    baseline = [_make_asset("home.png")]
    new = [_make_asset("home.png")]

    with (
        patch(
            "snap_dashboard.testing.baselines.get_or_build_stable_baseline_assets",
            return_value=baseline,
        ),
        patch(
            "snap_dashboard.testing.baselines.load_test_run_screenshots",
            return_value=new,
        ),
    ):
        context = _build_review_context(
            user_id=1,
            snap_name="evince",
            architecture="amd64",
            pr_number=None,
            test_run_id=42,
            review_decision="approve",
            review_confidence=0.9,
            review_reasoning="Looks good.",
            effective_repo="owner/repo",
            uc=SimpleNamespace(github_token="tok"),
        )

    assert context["has_baseline"] is True
    assert len(context["screenshot_pairs"]) == 1
    assert context["unpaired_screenshots"] == []
    assert context["review_decision"] == "approve"
    assert context["review_confidence"] == 0.9


def test_build_review_context_no_baseline_shows_unpaired_screenshots():
    """First-ever run for a snap: no baseline yet, but new screenshots
    should still be surfaced (the exact bug report — a passed run showed
    no way to view its screenshot)."""
    new = [_make_asset("main.png")]

    with (
        patch(
            "snap_dashboard.testing.baselines.get_or_build_stable_baseline_assets",
            return_value=[],
        ),
        patch(
            "snap_dashboard.testing.baselines.load_test_run_screenshots",
            return_value=new,
        ),
    ):
        context = _build_review_context(
            user_id=1,
            snap_name="evince",
            architecture="amd64",
            pr_number=None,
            test_run_id=42,
            review_decision=None,
            review_confidence=None,
            review_reasoning="Review skipped: no stable baseline yet.",
            effective_repo="owner/repo",
            uc=SimpleNamespace(github_token="tok"),
        )

    assert context["has_baseline"] is False
    assert context["screenshot_pairs"] == []
    assert len(context["unpaired_screenshots"]) == 1
    assert context["unpaired_screenshots"][0].image_name == "main.png"
