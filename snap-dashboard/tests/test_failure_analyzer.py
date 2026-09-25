"""Regression tests for LLM-inferred test-failure root-cause analysis.

TestRun.error_msg is a short raw message and log_output is the full raw
runner log — neither was ever summarized into a plain-English root cause,
so triaging a failed run meant reading the raw log by hand. These tests
cover TestFailureAnalyzerAgent (agents/test_failure_analyzer.py) and its
submission gate (testing.orchestrator.submit_test_run_failure_analysis).
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from snap_dashboard.agents import test_failure_analyzer as tfa
from snap_dashboard.db.models import Base, TestRun, User


@pytest.fixture
def isolated_session(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

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

    monkeypatch.setattr(tfa, "get_session", _fake_get_session)
    return session_local


class _FakeLemonadeClient:
    def __init__(self, reply="The application crashed during launch due to a missing library."):
        self.reply = reply
        self.prompts: list[str] = []

    def chat(self, prompt, temperature=0.2, max_tokens=None):
        self.prompts.append(prompt)
        return self.reply


def _make_failed_run(session_local, log_output="", error_msg="", status="failed"):
    session = session_local()
    user = User(github_login="ken", github_id=1)
    session.add(user)
    session.commit()
    run = TestRun(
        user_id=user.id,
        snap_name="godot-4",
        from_channel="candidate",
        version="4.5",
        status=status,
        log_output=log_output,
        error_msg=error_msg,
    )
    session.add(run)
    session.commit()
    run_id = run.id
    session.close()
    return run_id


def test_analyzer_records_summary_for_failed_run(isolated_session, monkeypatch):
    run_id = _make_failed_run(
        isolated_session,
        log_output="Traceback...\nGLXBadDrawable: missing GL driver\n",
    )
    fake_client = _FakeLemonadeClient()
    monkeypatch.setattr(tfa.TestFailureAnalyzerAgent, "_get_lemonade", lambda self, *a, **k: fake_client)
    monkeypatch.setattr(tfa, "get_user_config", lambda uid: object())

    agent = tfa.TestFailureAnalyzerAgent(test_run_id=run_id, user_id=1)
    result = agent._run()

    assert "recorded" in result
    assert fake_client.prompts  # a prompt was actually sent
    with isolated_session() as session:
        run = session.query(TestRun).get(run_id)
        assert run.failure_analysis == fake_client.reply


def test_analyzer_skips_when_no_log_or_error(isolated_session, monkeypatch):
    run_id = _make_failed_run(isolated_session)
    monkeypatch.setattr(tfa, "get_user_config", lambda uid: object())

    agent = tfa.TestFailureAnalyzerAgent(test_run_id=run_id, user_id=1)
    result = agent._run()

    assert "nothing to analyze" in result
    with isolated_session() as session:
        run = session.query(TestRun).get(run_id)
        assert run.failure_analysis is None


def test_analyzer_skips_when_run_not_failed(isolated_session, monkeypatch):
    run_id = _make_failed_run(isolated_session, log_output="all good", status="passed")

    agent = tfa.TestFailureAnalyzerAgent(test_run_id=run_id, user_id=1)
    result = agent._run()

    assert "not failed" in result


def test_analyzer_handles_no_local_model_available(isolated_session, monkeypatch):
    run_id = _make_failed_run(isolated_session, log_output="boom")
    monkeypatch.setattr(tfa.TestFailureAnalyzerAgent, "_get_lemonade", lambda self, *a, **k: None)
    monkeypatch.setattr(tfa, "get_user_config", lambda uid: object())

    agent = tfa.TestFailureAnalyzerAgent(test_run_id=run_id, user_id=1)
    result = agent._run()

    assert "no local model available" in result


def test_submit_gate_requires_failed_status_and_log(monkeypatch):
    """submit_test_run_failure_analysis should only queue the agent for
    failed/errored runs that actually have something to analyze."""
    from snap_dashboard.testing import orchestrator

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    @contextmanager
    def _fake_get_session():
        session = session_local()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    monkeypatch.setattr(orchestrator, "get_session", _fake_get_session)

    session = session_local()
    user = User(github_login="ken", github_id=1)
    session.add(user)
    session.commit()

    passed_run = TestRun(user_id=user.id, snap_name="a", from_channel="candidate", status="passed")
    failed_no_log = TestRun(user_id=user.id, snap_name="b", from_channel="candidate", status="failed")
    failed_with_log = TestRun(user_id=user.id, snap_name="c", from_channel="candidate", status="failed", log_output="oops")
    session.add_all([passed_run, failed_no_log, failed_with_log])
    session.commit()
    ids = (passed_run.id, failed_no_log.id, failed_with_log.id)
    session.close()

    submitted: list[int] = []
    monkeypatch.setattr(
        "snap_dashboard.agents.runner.get_runner",
        lambda: type("R", (), {"submit": staticmethod(lambda agent: submitted.append(agent.test_run_id))})(),
    )

    orchestrator.submit_test_run_failure_analysis(ids[0])
    orchestrator.submit_test_run_failure_analysis(ids[1])
    orchestrator.submit_test_run_failure_analysis(ids[2])

    assert submitted == [ids[2]]
