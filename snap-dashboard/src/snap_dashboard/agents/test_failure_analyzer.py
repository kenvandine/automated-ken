"""Test failure analyzer — infers a plain-English root cause for a failed test run."""

from __future__ import annotations

import logging

from snap_dashboard.agents.base import BaseAgent
from snap_dashboard.auth import get_user_config
from snap_dashboard.db.models import TestRun
from snap_dashboard.db.session import get_session

logger = logging.getLogger(__name__)

# The most useful failure signal (the actual error/traceback) is almost
# always near the *end* of a captured log, so tail it rather than
# truncating from the front — this also keeps the prompt small enough for
# a fast local-model turnaround.
_MAX_LOG_CHARS_FOR_ANALYSIS = 6000


class TestFailureAnalyzerAgent(BaseAgent):
    """Infers a plain-English root cause for a failed/errored test run from
    its captured runner log + error message, so a human doesn't have to
    dig through the raw log to understand why a run failed.

    Always attempted for every failed/errored run with a captured log —
    like screenshot review, this is useful output on its own regardless of
    any future auto-fix feature. It's deliberately purely informational
    for now (see TestRun.failure_analysis); a later agent could use this
    same analysis to attempt an automated fix PR, but that's out of scope
    here.

    Only covers runs dispatched to a snap-dashboard-managed runner (see
    web/routes/runner_api.update_job), since that's the only path that
    captures a real log today — externally-triggered PR/GitHub-Actions
    test runs don't have a log_output to analyze yet.
    """

    agent_type = "test_failure_analyzer"

    def __init__(self, test_run_id: int, user_id: int | None = None) -> None:
        super().__init__(user_id=user_id)
        self.test_run_id = test_run_id

    def _run(self) -> str:
        with get_session() as session:
            run = session.query(TestRun).get(self.test_run_id)
            if not run:
                return f"missing TestRun {self.test_run_id}"
            if run.status not in ("failed", "error"):
                return f"{run.snap_name}: not failed — skipping analysis"
            snap_name = run.snap_name
            version = run.version or ""
            log_output = run.log_output or ""
            error_msg = run.error_msg or ""
            user_id = run.user_id or self.user_id

        if not log_output and not error_msg:
            return f"{snap_name}: no log/error captured — nothing to analyze"

        uc = get_user_config(user_id) if user_id else None
        lemonade = self._get_lemonade(uc, task="text")
        if not lemonade:
            return f"{snap_name}: no local model available for failure analysis"

        excerpt = log_output[-_MAX_LOG_CHARS_FOR_ANALYSIS:] if log_output else ""
        prompt = (
            f"A test run failed for the snap '{snap_name}' (version {version}).\n\n"
            + (f"Reported error: {error_msg}\n\n" if error_msg else "")
            + (f"Captured runner log (tail):\n```\n{excerpt}\n```\n\n" if excerpt else "")
            + "In 2-4 sentences, explain the most likely root cause of this "
            "failure (for example: a snap build/install error, a crashed "
            "application, a missing dependency, a timeout, or a bug in the "
            "test suite itself) in plain English for someone triaging test "
            "failures. Be specific about what in the log points to that "
            "cause."
        )
        summary = lemonade.chat(prompt, temperature=0.2, max_tokens=300)
        if not summary:
            return f"{snap_name}: failure analysis call failed"

        with get_session() as session:
            run = session.query(TestRun).get(self.test_run_id)
            if run:
                run.failure_analysis = summary.strip()

        return f"{snap_name}: failure analysis recorded"
