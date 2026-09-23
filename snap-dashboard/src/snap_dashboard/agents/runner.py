"""Agent runner — thread pool for background agent execution and scheduling."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from snap_dashboard.agents.base import BaseAgent

logger = logging.getLogger(__name__)

_MAX_WORKERS = 4


class ActivityTracker:
    """Thread-safe in-memory log of live agent activity.

    Agents call ``set_active`` / ``clear_active`` to broadcast what they are
    doing right now.  The SSE endpoint reads ``get_log_since`` to push updates
    to connected browsers without touching the database.
    """

    def __init__(self, maxlen: int = 200) -> None:
        self._lock = threading.Lock()
        # agent_type → {task, snap_name, started_at (monotonic)}
        self._active: dict[str, dict] = {}
        self._log: deque = deque(maxlen=maxlen)
        self._seq = 0  # monotonically increasing, used as SSE cursor

    def set_active(self, agent_type: str, task: str, snap_name: str = "") -> None:
        with self._lock:
            self._active[agent_type] = {
                "task": task,
                "snap_name": snap_name,
                "started_at": time.monotonic(),
            }
            self._seq += 1
            self._log.append({
                "seq": self._seq,
                "ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                "agent_type": agent_type,
                "message": task,
                "snap_name": snap_name,
            })

    def clear_active(self, agent_type: str) -> None:
        with self._lock:
            self._active.pop(agent_type, None)

    def get_active(self) -> dict:
        with self._lock:
            return dict(self._active)

    def get_log_since(self, seq: int) -> list[dict]:
        with self._lock:
            return [e for e in self._log if e["seq"] > seq]

    def latest_seq(self) -> int:
        with self._lock:
            return self._seq


# Module-level tracker shared across all agents
_tracker = ActivityTracker()


def get_tracker() -> ActivityTracker:
    return _tracker


class AgentRunner:
    """Singleton thread pool that runs agents in the background.

    Usage::

        runner = get_runner()
        runner.submit(MyAgent(user_id=1))
        runner.schedule_periodic(ReleaseScannerAgent, interval_hours=4, user_id=1)
        runner.schedule_periodic(ReleaseScannerAgent, interval_hours=4,
                                  fire_immediately=True, user_id=1)
    """

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="agent")
        self._scheduled: list[_PeriodicJob] = []
        self._scheduler_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Submit a one-shot agent
    # ------------------------------------------------------------------

    def get_schedules(self) -> list[dict]:
        """Return info about all registered periodic schedules."""
        now = time.monotonic()
        result = []
        for job in self._scheduled:
            elapsed = now - job.last_run
            next_in = max(0.0, job.interval_seconds - elapsed)
            result.append({
                "agent_type": job.agent_cls.agent_type,
                "user_id": job.user_id,
                "interval_hours": job.interval_seconds / 3600,
                "next_run_in_seconds": int(next_in),
            })
        return result

    def submit(self, agent: "BaseAgent") -> None:
        """Submit an agent for immediate background execution."""
        self._executor.submit(_safe_run, agent)

    def is_scheduled(self, agent_cls: type, user_id: int | None = None) -> bool:
        """Return True if a periodic job already exists for (agent_cls, user_id)."""
        return any(
            job.agent_cls is agent_cls and job.user_id == user_id
            for job in self._scheduled
        )

    # ------------------------------------------------------------------
    # Periodic scheduling
    # ------------------------------------------------------------------

    def schedule_periodic(
        self,
        agent_cls: type,
        interval_hours: float,
        fire_immediately: bool = False,
        **kwargs,
    ) -> None:
        """Register an agent class to run every ``interval_hours`` hours.

        ``kwargs`` are forwarded to the agent constructor on each invocation.
        When ``fire_immediately`` is True the agent is submitted for execution
        right away instead of waiting for the first full interval to elapse.
        """
        job = _PeriodicJob(
            agent_cls=agent_cls,
            interval_hours=interval_hours,
            kwargs=kwargs,
            fire_immediately=fire_immediately,
        )
        self._scheduled.append(job)
        logger.info(
            "scheduled %s every %.1fh%s",
            agent_cls.__name__,
            interval_hours,
            " (firing immediately)" if fire_immediately else "",
        )
        if fire_immediately:
            try:
                agent = agent_cls(**kwargs)
                self._executor.submit(_safe_run, agent)
            except Exception as exc:
                logger.error("failed to submit immediate %s: %s", agent_cls.__name__, exc)

        if self._scheduler_thread is None or not self._scheduler_thread.is_alive():
            self._start_scheduler()

    def reschedule(self, agent_cls: type, interval_hours: float, **kwargs) -> None:
        """Update or add a periodic schedule for the given agent class + user.

        Jobs are matched by ``(agent_cls, user_id)`` — not agent_cls alone —
        so that changing one user's settings can never overwrite another
        user's scheduled job in a multi-tenant deployment.
        """
        user_id = kwargs.get("user_id")
        for job in self._scheduled:
            if job.agent_cls is agent_cls and job.user_id == user_id:
                job.interval_seconds = interval_hours * 3600
                job.kwargs = kwargs
                logger.info(
                    "rescheduled %s (user_id=%s) every %.1fh",
                    agent_cls.__name__, user_id, interval_hours,
                )
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
    def __init__(
        self,
        agent_cls: type,
        interval_hours: float,
        kwargs: dict,
        fire_immediately: bool = False,
    ) -> None:
        self.agent_cls = agent_cls
        self.interval_seconds = interval_hours * 3600
        self.kwargs = kwargs
        # user_id (if any) is tracked separately so jobs can be matched by
        # (agent_cls, user_id) instead of agent_cls alone — otherwise
        # rescheduling one user's agent could silently hijack another
        # user's job in a multi-tenant deployment.
        self.user_id = kwargs.get("user_id")
        # When fire_immediately=True the first submission is done inline by
        # schedule_periodic(); set last_run to now so the scheduler doesn't
        # double-fire before the interval elapses.
        # When fire_immediately=False, also start from now so the first
        # scheduled run fires after one full interval (original behaviour).
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
