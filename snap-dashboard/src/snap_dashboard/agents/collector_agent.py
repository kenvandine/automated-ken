"""Collector agent — periodically refreshes channel maps and issue/PR data.

Wraps :func:`snap_dashboard.collector.run_collection` so that Snap Store
channel data (stable/candidate/beta/edge versions) and GitHub/GitLab
issue/PR counts stay fresh automatically, the same way the release scanner
and stale-build scanner already do. Previously this data was only ever
refreshed by the manual "Refresh now" button or once during onboarding,
so the dashboard could silently go stale for anyone who forgot to click it.
"""

from __future__ import annotations

import logging

from snap_dashboard.agents.base import BaseAgent
from snap_dashboard.auth import get_user_config
from snap_dashboard.db.session import get_session

logger = logging.getLogger(__name__)


class CollectorAgent(BaseAgent):
    """Runs the Snap Store / GitHub collection pipeline for one user."""

    agent_type = "collector"

    def __init__(self, user_id: int | None = None) -> None:
        super().__init__(user_id=user_id)

    def _run(self) -> str:
        from snap_dashboard.collector import run_collection

        uc = get_user_config(self.user_id) if self.user_id else None
        if not uc or not uc.publisher:
            return "skipped: no publisher configured"

        self._report(f"Refreshing channel maps and issues for publisher {uc.publisher}…")
        config = uc.to_config()
        with get_session() as session:
            summary = run_collection(session, config, user_id=self.user_id)

        return (
            f"status={summary['status']} "
            f"snaps_updated={summary['snaps_updated']} "
            f"issues_updated={summary['issues_updated']}"
            + (f" error={summary['error']}" if summary.get("error") else "")
        )
