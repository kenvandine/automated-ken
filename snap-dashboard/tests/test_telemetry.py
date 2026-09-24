"""Tests for LLM token-usage tracking (snap_dashboard.telemetry).

Covers the model-agnostic estimate heuristic and the best-effort
``record_model_usage()`` persistence used by both the local (lemonade) and
cloud (Copilot cloud agent) call sites.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from snap_dashboard import telemetry
from snap_dashboard.db.models import Base, ModelUsage


@pytest.fixture
def isolated_session(monkeypatch):
    """Point db.session.get_session (as imported lazily) at a throwaway DB."""
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

    # record_model_usage() imports get_session lazily from
    # snap_dashboard.db.session, so patch it there.
    monkeypatch.setattr("snap_dashboard.db.session.get_session", _fake_get_session)
    return session_local


def test_empty_text_estimates_zero_tokens() -> None:
    assert telemetry.estimate_tokens("") == 0
    assert telemetry.estimate_tokens(None) == 0


def test_short_text_estimates_at_least_one_token() -> None:
    assert telemetry.estimate_tokens("hi") == 1


def test_estimate_is_roughly_four_chars_per_token() -> None:
    assert telemetry.estimate_tokens("a" * 400) == 100


def test_record_model_usage_persists_a_row(isolated_session) -> None:
    telemetry.record_model_usage(
        provider="lemonade",
        model="user.Qwen3.5-35B-A3B-Q4_K_M",
        task="chat",
        input_tokens=10,
        output_tokens=20,
        estimated=False,
    )
    with isolated_session() as session:
        rows = session.query(ModelUsage).all()
    assert len(rows) == 1
    assert rows[0].provider == "lemonade"
    assert rows[0].input_tokens == 10
    assert rows[0].output_tokens == 20
    assert rows[0].estimated is False


def test_record_model_usage_clamps_negative_counts_to_zero(isolated_session) -> None:
    telemetry.record_model_usage(
        provider="copilot", model="m", task="coding_agent",
        input_tokens=-5, output_tokens=-1,
    )
    with isolated_session() as session:
        row = session.query(ModelUsage).one()
    assert row.input_tokens == 0
    assert row.output_tokens == 0


def test_record_model_usage_never_raises_when_db_is_unavailable(monkeypatch) -> None:
    def _broken_get_session():
        raise RuntimeError("db unavailable")

    monkeypatch.setattr("snap_dashboard.db.session.get_session", _broken_get_session)
    # Should log and swallow the error, not raise.
    telemetry.record_model_usage("lemonade", "m", "chat", 1, 1)
