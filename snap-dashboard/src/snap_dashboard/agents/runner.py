"""Agent runner — thread pool for background agent execution and scheduling."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from snap_dashboard.agents.base import BaseAgent

logger = logging.getLogger(__name__)

_MAX_WORKERS = 4


class AgentRunner:
    """Singleton thread pool that runs agents in the background.

    Usage::

        runner = get_runner()
        runner.submit(MyAgent(user_id=1))
        runner.schedule_periodic(ReleaseScannerAgent, interval_hours=4, user_id=1)
    """

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="agent")
        self._scheduled: list[_PeriodicJob] = []
        self._scheduler_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Submit a one-shot agent
    # ------------------------------------------------------------------

    def submit(self, agent: "BaseAgent") -> None:
        """Submit an agent for immediate background execution."""
        self._executor.submit(_safe_run, agent)

    # ------------------------------------------------------------------
    # Periodic scheduling
    # ------------------------------------------------------------------

    def schedule_periodic(
        self,
        agent_cls: type,
        interval_hours: float,
        **kwargs,
    ) -> None:
        """Register an agent class to run every ``interval_hours`` hours.

        ``kwargs`` are forwarded to the agent constructor on each invocation.
        """
        job = _PeriodicJob(agent_cls=agent_cls, interval_hours=interval_hours, kwargs=kwargs)
        self._scheduled.append(job)
        logger.info(
            "scheduled %s every %.1fh", agent_cls.__name__, interval_hours
        )
        if self._scheduler_thread is None or not self._scheduler_thread.is_alive():
            self._start_scheduler()

    def reschedule(self, agent_cls: type, interval_hours: float, **kwargs) -> None:
        """Update or add a periodic schedule for the given agent class."""
        for job in self._scheduled:
            if job.agent_cls is agent_cls:
                job.interval_seconds = interval_hours * 3600
                job.kwargs = kwargs
                logger.info("rescheduled %s every %.1fh", agent_cls.__name__, interval_hours)
                return
        self.schedule_periodic(agent_cls, interval_hours, **kwargs)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        self._stop_event.set()
        self._executor.shutdown(wait=False)

    def _start_scheduler(self) -> None:
        self._stop_event.clear()
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop,
            daemon=True,
            name="agent-scheduler",
        )
        self._scheduler_thread.start()

    def _scheduler_loop(self) -> None:
        while not self._stop_event.is_set():
            now = time.monotonic()
            for job in self._scheduled:
                if now - job.last_run >= job.interval_seconds:
                    job.last_run = now
                    try:
                        agent = job.agent_cls(**job.kwargs)
                        self._executor.submit(_safe_run, agent)
                    except Exception as exc:
                        logger.error("failed to submit %s: %s", job.agent_cls.__name__, exc)
            self._stop_event.wait(timeout=60)  # wake every minute to check schedules


class _PeriodicJob:
    def __init__(self, agent_cls: type, interval_hours: float, kwargs: dict) -> None:
        self.agent_cls = agent_cls
        self.interval_seconds = interval_hours * 3600
        self.kwargs = kwargs
        # Set last_run so the first execution fires after one full interval.
        self.last_run = time.monotonic()


def _safe_run(agent: "BaseAgent") -> None:
    try:
        agent.run()
    except Exception as exc:
        logger.exception("unhandled error in agent %s: %s", type(agent).__name__, exc)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_runner: AgentRunner | None = None
_runner_lock = threading.Lock()


def get_runner() -> AgentRunner:
    """Return the global AgentRunner, creating it on first call."""
    global _runner
    if _runner is None:
        with _runner_lock:
            if _runner is None:
                _runner = AgentRunner()
    return _runner
