"""Tests for the per-user agent scheduler.

Regression coverage for a bug where ``AgentRunner.reschedule()`` matched
jobs by agent class only. In a multi-tenant deployment, changing one user's
settings would silently overwrite another user's already-scheduled job
(reassigning it to the wrong user_id) instead of creating/updating its own.
"""

from __future__ import annotations

from snap_dashboard.agents.collector_agent import CollectorAgent
from snap_dashboard.agents.release_scanner import ReleaseScannerAgent
from snap_dashboard.agents.runner import AgentRunner
from snap_dashboard.agents.scheduling import schedule_user_agents
from snap_dashboard.agents.stale_build_scanner import StaleSnapScannerAgent


class _FakeUserConfig:
    def __init__(self, agent_interval_hours: float = 4, collect_interval_hours: float = 6) -> None:
        self.agent_interval_hours = agent_interval_hours
        self.collect_interval_hours = collect_interval_hours


def _make_runner() -> AgentRunner:
    runner = AgentRunner()
    # Don't let the background scheduler thread run during tests — we only
    # exercise the scheduling data structures, not real execution timing.
    return runner


def test_schedule_user_agents_creates_one_job_set_per_user() -> None:
    runner = _make_runner()
    try:
        schedule_user_agents(runner, user_id=1, uc=_FakeUserConfig())
        schedule_user_agents(runner, user_id=2, uc=_FakeUserConfig())

        assert runner.is_scheduled(ReleaseScannerAgent, user_id=1)
        assert runner.is_scheduled(ReleaseScannerAgent, user_id=2)
        assert runner.is_scheduled(CollectorAgent, user_id=1)
        assert runner.is_scheduled(StaleSnapScannerAgent, user_id=2)
        assert len(runner._scheduled) == 6
    finally:
        runner.shutdown()


def test_schedule_user_agents_is_idempotent_per_user() -> None:
    """Calling it again for the same user updates in place, no duplicates."""
    runner = _make_runner()
    try:
        schedule_user_agents(runner, user_id=1, uc=_FakeUserConfig(agent_interval_hours=4))
        schedule_user_agents(runner, user_id=1, uc=_FakeUserConfig(agent_interval_hours=8))

        assert len(runner._scheduled) == 3
        job = next(j for j in runner._scheduled if j.agent_cls is ReleaseScannerAgent)
        assert job.interval_seconds == 8 * 3600
    finally:
        runner.shutdown()


def test_rescheduling_one_user_does_not_affect_another() -> None:
    """The bug this guards against: reschedule() used to match by class only."""
    runner = _make_runner()
    try:
        schedule_user_agents(runner, user_id=1, uc=_FakeUserConfig(agent_interval_hours=4))
        schedule_user_agents(runner, user_id=2, uc=_FakeUserConfig(agent_interval_hours=4))

        runner.reschedule(ReleaseScannerAgent, interval_hours=12, user_id=1)

        jobs = {j.user_id: j for j in runner._scheduled if j.agent_cls is ReleaseScannerAgent}
        assert jobs[1].interval_seconds == 12 * 3600
        assert jobs[2].interval_seconds == 4 * 3600  # untouched
        assert jobs[1].user_id == 1
        assert jobs[2].user_id == 2
    finally:
        runner.shutdown()
