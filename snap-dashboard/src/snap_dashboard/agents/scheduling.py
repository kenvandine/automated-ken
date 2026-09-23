"""Central place to (re)schedule every per-user periodic agent.

This is the single source of truth for "what background agents should be
running for user X, and at what interval". It is used from three places
that all need to agree with each other:

- ``web/app.py`` on startup, for every user that already exists.
- ``web/routes/onboarding.py`` right after a *new* user finishes onboarding
  (previously this never happened, so a fresh install's agents silently
  never started until the whole daemon was restarted).
- ``web/routes/settings.py`` whenever a user changes their interval
  settings.

Scheduling is idempotent: calling this for a user that's already scheduled
just updates the interval (via ``AgentRunner.reschedule``) instead of
creating a duplicate job.
"""

from __future__ import annotations

import logging

from snap_dashboard.agents.collector_agent import CollectorAgent
from snap_dashboard.agents.release_scanner import ReleaseScannerAgent
from snap_dashboard.agents.runner import AgentRunner
from snap_dashboard.agents.stale_build_scanner import StaleSnapScannerAgent

logger = logging.getLogger(__name__)

# Stale-build scanning always runs once a day; only the release scan and
# collection intervals are user-configurable.
_STALE_SCAN_INTERVAL_HOURS = 24


def schedule_user_agents(
    runner: AgentRunner,
    user_id: int,
    uc,
    fire_immediately: bool = False,
) -> None:
    """Ensure every periodic per-user agent is scheduled for ``user_id``.

    ``uc`` is a UserConfig-like object (has ``agent_interval_hours`` and
    ``collect_interval_hours``). Safe to call repeatedly — existing jobs for
    this user are updated in place rather than duplicated.
    """
    release_interval = getattr(uc, "agent_interval_hours", None) or 4
    collect_interval = getattr(uc, "collect_interval_hours", None) or 6

    # fire_immediately only makes sense the first time a job is created —
    # reschedule() leaves last_run untouched for existing jobs, so calling
    # this repeatedly (e.g. from settings) won't cause repeated immediate runs.
    is_new_release_job = not runner.is_scheduled(ReleaseScannerAgent, user_id)
    is_new_collector_job = not runner.is_scheduled(CollectorAgent, user_id)
    is_new_stale_job = not runner.is_scheduled(StaleSnapScannerAgent, user_id)

    if is_new_release_job:
        runner.schedule_periodic(
            ReleaseScannerAgent,
            interval_hours=release_interval,
            fire_immediately=fire_immediately,
            user_id=user_id,
        )
    else:
        runner.reschedule(ReleaseScannerAgent, interval_hours=release_interval, user_id=user_id)

    if is_new_collector_job:
        runner.schedule_periodic(
            CollectorAgent,
            interval_hours=collect_interval,
            fire_immediately=fire_immediately,
            user_id=user_id,
        )
    else:
        runner.reschedule(CollectorAgent, interval_hours=collect_interval, user_id=user_id)

    if is_new_stale_job:
        runner.schedule_periodic(
            StaleSnapScannerAgent,
            interval_hours=_STALE_SCAN_INTERVAL_HOURS,
            user_id=user_id,
        )
    # Stale scan interval is not user-configurable, so nothing to reschedule.

    logger.info(
        "agents scheduled for user_id=%s (release=%.1fh, collect=%.1fh, stale=%dh)",
        user_id, release_interval, collect_interval, _STALE_SCAN_INTERVAL_HOURS,
    )
