"""Runner watchdog agent — detects and clears stalled remote-runner jobs.

A job can get stuck if a runner machine crashes, loses network, or its
YARF process hangs without ever reporting a terminal status back. This
agent finds ``TestRun`` rows dispatched to a remote runner that have been
``triggered``/``running`` for longer than the user's configured timeout
and force-fails them, freeing up the runner for its next job. Manual
"kick out this job" from the /runners page does the same thing
immediately via ``web/routes/runners.py::cancel_job``; this agent is the
automatic backstop for jobs nobody noticed were stuck.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from snap_dashboard.agents.base import BaseAgent
from snap_dashboard.auth import get_user_config
from snap_dashboard.db.models import Runner, TestRun
from snap_dashboard.db.session import get_session

logger = logging.getLogger(__name__)

_IN_FLIGHT = ("triggered", "running")


class RunnerWatchdogAgent(BaseAgent):
    """Sweeps for stalled remote-runner jobs across all users."""

    agent_type = "runner_watchdog"

    def __init__(self, user_id: int | None = None) -> None:
        super().__init__(user_id=user_id)

    def _run(self) -> str:
        with get_session() as session:
            q = (
                session.query(TestRun)
                .filter_by(dispatch_target="remote_runner")
                .filter(TestRun.status.in_(_IN_FLIGHT))
            )
            if self.user_id:
                q = q.filter_by(user_id=self.user_id)
            jobs = [
                {"id": j.id, "user_id": j.user_id, "started_at": j.started_at, "runner_id": j.runner_id}
                for j in q.all()
            ]

        cleared = 0
        now = datetime.now(timezone.utc)
        for job in jobs:
            timeout_minutes = 10
            if job["user_id"]:
                uc = get_user_config(job["user_id"])
                timeout_minutes = getattr(uc, "runner_job_timeout_minutes", 10) or 10
            started = job["started_at"]
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            deadline = started + timedelta(minutes=timeout_minutes)
            if now < deadline:
                continue
            self._force_fail(job["id"], job["runner_id"])
            cleared += 1

        return f"checked {len(jobs)} in-flight remote-runner job(s), cleared {cleared} stalled"

    @staticmethod
    def _force_fail(job_id: int, runner_id: int | None) -> None:
        with get_session() as session:
            job = session.query(TestRun).get(job_id)
            if job is None or job.status not in _IN_FLIGHT:
                return
            job.status = "failed"
            job.error_msg = "Runner job timed out (no status update within the configured timeout)."
            job.finished_at = datetime.now(timezone.utc)
            if runner_id:
                runner = session.query(Runner).get(runner_id)
                if runner is not None and runner.current_test_run_id == job_id:
                    runner.current_test_run_id = None
                    runner.status = "offline"  # presumed unresponsive — heartbeat will correct this if wrong
        logger.info("runner_watchdog: force-failed stalled job %s (runner %s)", job_id, runner_id)
