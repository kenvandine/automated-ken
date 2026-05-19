"""Base class for all background agents."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from snap_dashboard.db.models import AgentRun
from snap_dashboard.db.session import get_session

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    """Abstract base for agents.  Subclasses implement ``_run()``."""

    #: Override in subclass to identify the agent type in the DB.
    agent_type: str = "unknown"

    def __init__(self, user_id: int | None = None, snap_name: str | None = None) -> None:
        self.user_id = user_id
        self.snap_name = snap_name
        self._run_id: int | None = None

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Execute the agent, recording start/end/error to agent_runs."""
        self._run_id = self._start_run()
        self._report("Starting…")
        logger.info("agent %s started (run_id=%s snap=%s)", self.agent_type, self._run_id, self.snap_name)
        try:
            summary = self._run()
            self._finish_run(summary=summary)
            logger.info("agent %s done (run_id=%s): %s", self.agent_type, self._run_id, summary)
        except Exception as exc:
            logger.exception("agent %s error (run_id=%s): %s", self.agent_type, self._run_id, exc)
            self._error_run(str(exc))
        finally:
            from snap_dashboard.agents.runner import get_tracker
            get_tracker().clear_active(self.agent_type)

    # ------------------------------------------------------------------
    # Subclass interface
    # ------------------------------------------------------------------

    @abstractmethod
    def _run(self) -> str:
        """Perform the agent's work.  Return a short human-readable summary."""

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    def _start_run(self) -> int:
        with get_session() as session:
            run = AgentRun(
                user_id=self.user_id,
                agent_type=self.agent_type,
                snap_name=self.snap_name,
                status="running",
            )
            session.add(run)
            session.flush()
            return run.id

    def _finish_run(self, summary: str) -> None:
        if self._run_id is None:
            return
        with get_session() as session:
            run = session.query(AgentRun).get(self._run_id)
            if run:
                run.status = "done"
                run.result_summary = summary
                run.finished_at = datetime.now(timezone.utc)

    def _error_run(self, error_msg: str) -> None:
        if self._run_id is None:
            return
        with get_session() as session:
            run = session.query(AgentRun).get(self._run_id)
            if run:
                run.status = "error"
                run.error_msg = error_msg
                run.finished_at = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # Progress reporting (shown live in the dashboard)
    # ------------------------------------------------------------------

    def _report(self, message: str, snap_name: str | None = None) -> None:
        """Broadcast what this agent is doing right now to the activity tracker."""
        from snap_dashboard.agents.runner import get_tracker
        get_tracker().set_active(
            self.agent_type,
            message,
            snap_name=snap_name if snap_name is not None else (self.snap_name or ""),
        )

    # ------------------------------------------------------------------
    # Lemonade helper
    # ------------------------------------------------------------------

    def _get_lemonade(self, user_config=None):
        """Return a LemonadeClient if configured and available, else None."""
        from snap_dashboard.lemonade.client import get_lemonade_client
        client = get_lemonade_client(user_config)
        if client and client.is_available():
            return client
        return None
