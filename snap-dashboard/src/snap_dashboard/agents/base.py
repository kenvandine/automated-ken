"""Base class for all background agents."""

from __future__ import annotations

import io
import logging
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from snap_dashboard.db.models import AgentRun
from snap_dashboard.db.session import get_session, retry_on_db_lock

logger = logging.getLogger(__name__)

# Cap how much log text we keep per run so a runaway/looping agent can't
# blow up the sqlite row (and the browser rendering it) with unbounded
# output — keep the tail, since that's what you want when debugging a run
# that got stuck or errored out at the end.
_MAX_LOG_CHARS = 200_000


class _ThreadLogCapture(logging.Handler):
    """Captures every log record emitted by one specific thread.

    Agents run on a shared ``ThreadPoolExecutor`` (see agents/runner.py), so
    several agents' code can be logging concurrently on the root logger at
    once. Filtering by ``record.thread`` (the emitting thread's ident) means
    each agent's captured log only contains records from its own run, even
    though the handler is attached process-wide for the run's duration.
    """

    def __init__(self, thread_ident: int) -> None:
        super().__init__(level=logging.INFO)
        self._thread_ident = thread_ident
        self._buffer = io.StringIO()
        self.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
        )

    def filter(self, record: logging.LogRecord) -> bool:
        return record.thread == self._thread_ident

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._buffer.write(self.format(record) + "\n")
        except Exception:
            pass  # never let logging itself break the agent run

    def getvalue(self) -> str:
        text = self._buffer.getvalue()
        if len(text) > _MAX_LOG_CHARS:
            text = "…(truncated)…\n" + text[-_MAX_LOG_CHARS:]
        return text


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
        capture = _ThreadLogCapture(threading.get_ident())
        root_logger = logging.getLogger()
        root_logger.addHandler(capture)
        try:
            self._report("Starting…")
            logger.info("agent %s started (run_id=%s snap=%s)", self.agent_type, self._run_id, self.snap_name)
            try:
                summary = self._run()
                self._finish_run(summary=summary, log_output=capture.getvalue())
                logger.info("agent %s done (run_id=%s): %s", self.agent_type, self._run_id, summary)
            except Exception as exc:
                logger.exception("agent %s error (run_id=%s): %s", self.agent_type, self._run_id, exc)
                self._error_run(str(exc), log_output=capture.getvalue())
            finally:
                from snap_dashboard.agents.runner import get_tracker
                get_tracker().clear_active(id(self))
        finally:
            root_logger.removeHandler(capture)

    # ------------------------------------------------------------------
    # Subclass interface
    # ------------------------------------------------------------------

    @abstractmethod
    def _run(self) -> str:
        """Perform the agent's work.  Return a short human-readable summary."""

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    @retry_on_db_lock()
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

    @retry_on_db_lock()
    def _finish_run(self, summary: str, log_output: str = "") -> None:
        if self._run_id is None:
            return
        with get_session() as session:
            run = session.query(AgentRun).get(self._run_id)
            if run:
                run.status = "done"
                run.result_summary = summary
                run.log_output = log_output or None
                run.finished_at = datetime.now(timezone.utc)

    @retry_on_db_lock()
    def _error_run(self, error_msg: str, log_output: str = "") -> None:
        if self._run_id is None:
            return
        with get_session() as session:
            run = session.query(AgentRun).get(self._run_id)
            if run:
                run.status = "error"
                run.error_msg = error_msg
                run.log_output = log_output or None
                run.finished_at = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # Progress reporting (shown live in the dashboard)
    # ------------------------------------------------------------------

    def _report(
        self, message: str, snap_name: str | None = None, user_id: int | None = None
    ) -> None:
        """Broadcast what this agent is doing right now to the activity tracker.

        ``user_id`` attributes the message to a user other than the agent's
        own — for agents that sweep across all users (e.g. the PR monitor)
        so a message about one user's snap isn't shown to everyone.
        """
        from snap_dashboard.agents.runner import get_tracker
        get_tracker().set_active(
            id(self),
            self.agent_type,
            message,
            snap_name=snap_name if snap_name is not None else (self.snap_name or ""),
            user_id=user_id if user_id is not None else self.user_id,
        )

    # ------------------------------------------------------------------
    # Lemonade helper
    # ------------------------------------------------------------------

    def _get_lemonade(self, user_config=None, task: str = "text"):
        """Return a LemonadeClient if configured and available, else None.

        ``task`` selects the opinionated default model for this call
        ("vision", "text", or "coding") — see ``lemonade.models.TASK_MODELS``.
        """
        from snap_dashboard.lemonade.client import get_lemonade_client
        client = get_lemonade_client(user_config, ensure_started=True, task=task)
        if client and client.is_available():
            return client
        return None
