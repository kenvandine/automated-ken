"""Tests for TestRunAutoPromoterAgent's "no baseline yet" screenshot-review
path — using vision_inspect() to judge a lone screenshot when there's
nothing to compare it against yet (e.g. the first tested version of a
snap). Previously this case was skipped entirely with a generic note and
no score was ever recorded, even though the run had passed and was
"ready to promote" (see the AI Screenshot Review card on run_detail.html
/ pr_detail.html / testing.html / dashboard.html).
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from snap_dashboard.agents import test_run_auto_promoter as promoter_mod
from snap_dashboard.db.models import Base, TestRun


@pytest.fixture
def isolated_session(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    @contextmanager
    def _fake_get_session():
        session = session_local()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(promoter_mod, "get_session", _fake_get_session)
    return session_local


def _asset(image_name: str) -> SimpleNamespace:
    return SimpleNamespace(image_name=image_name, image_bytes=b"fakepng")


def test_no_baseline_still_runs_vision_inspect_and_records_low_confidence_score(
    monkeypatch, isolated_session
):
    session_local = isolated_session

    with session_local() as session:
        run = TestRun(
            user_id=1,
            snap_name="evince",
            architecture="amd64",
            from_channel="candidate",
            version="45.0",
            revision=17,
            status="passed",
            triggered_by="manual",
        )
        session.add(run)
        session.commit()
        run_id = run.id

    monkeypatch.setattr(
        promoter_mod, "get_user_config", lambda uid: SimpleNamespace(
            github_token="tok", testing_repo="owner/repo",
            auto_promote_confidence=0.85, auto_promote=False,
            snapcraft_macaroon="",
        )
    )
    # No stable baseline at all.
    monkeypatch.setattr(promoter_mod, "get_or_build_stable_baseline_assets", lambda *a, **k: [])
    # But there is exactly one new screenshot from this run.
    new_asset = _asset("main.png")
    monkeypatch.setattr(
        promoter_mod, "load_test_run_screenshots", lambda *a, **k: [new_asset]
    )
    monkeypatch.setattr(promoter_mod, "pair_screenshots", lambda baseline, new: [])

    fake_lemonade = MagicMock()
    fake_lemonade.vision_inspect.return_value = {
        "decision": "approve",
        "confidence": 0.3,
        "reasoning": "A window with the application UI is visible.",
    }
    monkeypatch.setattr(
        promoter_mod.TestRunAutoPromoterAgent, "_get_lemonade", lambda self, *a, **k: fake_lemonade
    )

    agent = promoter_mod.TestRunAutoPromoterAgent(test_run_id=run_id, user_id=1)
    summary = agent._run()

    fake_lemonade.vision_inspect.assert_called_once_with(
        image_bytes=b"fakepng", snap_name="evince", version="45.0"
    )
    fake_lemonade.vision_compare.assert_not_called()
    assert "manual review" in summary

    with session_local() as session:
        updated = session.query(TestRun).get(run_id)
        assert updated.review_decision == "approve"
        assert updated.review_confidence == 0.3
        assert "window" in updated.review_reasoning.lower()
        # Confidence-capped single-image judging must never trigger
        # auto-promotion, even with auto_promote on — only a real
        # baseline comparison should be able to do that.
        assert updated.promoted is False


def test_no_screenshots_at_all_still_skips_gracefully(monkeypatch, isolated_session):
    session_local = isolated_session

    with session_local() as session:
        run = TestRun(
            user_id=1,
            snap_name="evince",
            architecture="amd64",
            from_channel="candidate",
            version="45.0",
            revision=17,
            status="passed",
            triggered_by="manual",
        )
        session.add(run)
        session.commit()
        run_id = run.id

    monkeypatch.setattr(
        promoter_mod, "get_user_config", lambda uid: SimpleNamespace(
            github_token="tok", testing_repo="owner/repo",
            auto_promote_confidence=0.85, auto_promote=False,
            snapcraft_macaroon="",
        )
    )
    monkeypatch.setattr(promoter_mod, "get_or_build_stable_baseline_assets", lambda *a, **k: [])
    monkeypatch.setattr(promoter_mod, "load_test_run_screenshots", lambda *a, **k: [])

    agent = promoter_mod.TestRunAutoPromoterAgent(test_run_id=run_id, user_id=1)
    summary = agent._run()

    assert "no comparable screenshots" in summary
    with session_local() as session:
        updated = session.query(TestRun).get(run_id)
        assert updated.review_decision is None
        assert "no screenshots are available" in (updated.review_reasoning or "")
