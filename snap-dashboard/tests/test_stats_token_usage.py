"""Tests for the /stats page's token-usage aggregation.

Exercises ``_aggregate_model_usage()`` (extracted from stats_page() for
testability) against an isolated in-memory database.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from snap_dashboard.db.models import Base, ModelUsage
from snap_dashboard.web.routes.stats import _aggregate_model_usage


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = session_local()
    try:
        yield s
    finally:
        s.close()


def test_no_usage_yields_empty_aggregates(session) -> None:
    result = _aggregate_model_usage(session)
    assert result["model_usage"] == []
    assert result["local_tokens"] == 0
    assert result["cloud_tokens"] == 0
    assert result["total_tokens"] == 0
    assert result["local_token_share"] is None


def test_aggregates_by_provider_and_model(session) -> None:
    session.add_all(
        [
            ModelUsage(
                provider="lemonade", model="qwen-vision", task="vision_compare",
                input_tokens=1000, output_tokens=200, estimated=False,
            ),
            ModelUsage(
                provider="lemonade", model="qwen-vision", task="vision_compare",
                input_tokens=500, output_tokens=100, estimated=True,
            ),
            ModelUsage(
                provider="copilot", model="gpt-5", task="coding_agent",
                input_tokens=300, output_tokens=0, estimated=True,
            ),
        ]
    )
    session.commit()

    result = _aggregate_model_usage(session)

    by_model = {(r["provider"], r["model"]): r for r in result["model_usage"]}
    lemonade_row = by_model[("lemonade", "qwen-vision")]
    assert lemonade_row["calls"] == 2
    assert lemonade_row["input_tokens"] == 1500
    assert lemonade_row["output_tokens"] == 300
    assert lemonade_row["total_tokens"] == 1800
    assert lemonade_row["any_estimated"] is True

    copilot_row = by_model[("copilot", "gpt-5")]
    assert copilot_row["input_tokens"] == 300
    assert copilot_row["output_tokens"] == 0

    # local (lemonade) vs. cloud (anything else) split
    assert result["local_tokens"] == 1800
    assert result["cloud_tokens"] == 300
    assert result["total_tokens"] == 2100
    assert result["local_token_share"] == pytest.approx(1800 / 2100 * 100, rel=1e-3)


def test_all_exact_usage_marks_any_estimated_false(session) -> None:
    session.add(
        ModelUsage(
            provider="lemonade", model="m", task="chat",
            input_tokens=10, output_tokens=5, estimated=False,
        )
    )
    session.commit()
    result = _aggregate_model_usage(session)
    assert result["model_usage"][0]["any_estimated"] is False
