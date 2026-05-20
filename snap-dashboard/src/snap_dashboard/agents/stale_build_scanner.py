"""Stale build scanner agent — finds snaps not published recently and triggers rebuilds."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from snap_dashboard.agents.base import BaseAgent
from snap_dashboard.auth import get_user_config
from snap_dashboard.db.models import ChannelMap, Snap, StaleBuildTrigger
from snap_dashboard.db.session import get_session
from snap_dashboard.snapcraft.build_workflow_template import WORKFLOW_FILENAME, WORKFLOW_PATH, WORKFLOW_YAML

logger = logging.getLogger(__name__)

_DEFAULT_STALE_DAYS = 30
_BUILD_WORKFLOW = WORKFLOW_FILENAME  # automated-snap-build.yml
_BUILD_CHANNEL = "candidate"


def _parse_github_owner_repo(repo_url: str) -> tuple[str, str] | None:
    """Return (owner, repo) from a GitHub URL, or None for non-GitHub URLs."""
    if not repo_url:
        return None
    parsed = urlparse(repo_url.rstrip("/"))
    if "github.com" not in parsed.netloc.lower():
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        return None
    return parts[0], parts[1].removesuffix(".git")


class StaleSnapScannerAgent(BaseAgent):
    """Finds snaps that haven't published a new revision in N days and triggers rebuilds.

    For each qualifying snap:
      1. Create automated-snap-build.yml in the packaging repo if it doesn't exist.
      2. Dispatch a workflow_dispatch event to publish to the candidate channel.
      3. Record the trigger in StaleBuildTrigger to avoid re-firing within the window.
    """

    agent_type = "stale_build_scanner"

    def __init__(self, user_id: int | None = None) -> None:
        super().__init__(user_id=user_id)

    def _run(self) -> str:
        uc = get_user_config(self.user_id) if self.user_id else None

        if not uc or not uc.auto_rebuild_stale:
            return "auto_rebuild_stale is disabled — skipping"

        token = (uc.github_token or "") if uc else ""
        if not token:
            return "no GitHub token configured — skipping"

        stale_days = (uc.stale_build_days or _DEFAULT_STALE_DAYS) if uc else _DEFAULT_STALE_DAYS
        cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)

        with get_session() as session:
            q = session.query(Snap)
            if self.user_id:
                q = q.filter_by(user_id=self.user_id)
            snaps = [
                {
                    "id": s.id,
                    "name": s.name,
                    "packaging_repo": s.packaging_repo or "",
                    "user_id": s.user_id,
                }
                for s in q.all()
                if s.packaging_repo
            ]

        total = len(snaps)
        self._report(f"Scanning {total} snaps for stale publications…")

        triggered = 0
        skipped = 0
        errors = 0

        for i, snap in enumerate(snaps, 1):
            self._report(
                f"Checking {i}/{total}: {snap['name']}",
                snap["name"],
            )
            result = self._check_and_trigger(snap, token, cutoff, stale_days)
            if result == "triggered":
                triggered += 1
            elif result == "skipped":
                skipped += 1
            elif result == "error":
                errors += 1

        return (
            f"scanned {total} snaps — "
            f"{triggered} build(s) triggered, {skipped} skipped, {errors} error(s)"
        )

    def _check_and_trigger(
        self,
        snap: dict,
        token: str,
        cutoff: datetime,
        stale_days: int,
    ) -> str:
        """Check one snap and trigger a rebuild if stale. Returns 'triggered', 'skipped', or 'error'."""
        owner_repo = _parse_github_owner_repo(snap["packaging_repo"])
        if not owner_repo:
            logger.debug("stale_build_scanner: %s has no GitHub packaging_repo, skipping", snap["name"])
            return "skipped"

        owner, repo = owner_repo

        with get_session() as session:
            # Find the most recent publication across all channels/architectures
            rows = session.query(ChannelMap).filter_by(snap_id=snap["id"]).all()
            if not rows:
                logger.debug("stale_build_scanner: %s has no channel map entries, skipping", snap["name"])
                return "skipped"

            # released_at can be None — treat None as epoch (very old)
            _epoch = datetime.fromtimestamp(0, tz=timezone.utc)
            latest_publish = max(
                (cm.released_at if cm.released_at else _epoch for cm in rows),
                default=_epoch,
            )
            # Ensure timezone-aware
            if latest_publish.tzinfo is None:
                latest_publish = latest_publish.replace(tzinfo=timezone.utc)

            if latest_publish >= cutoff:
                return "skipped"  # published recently enough

            days_stale = (datetime.now(timezone.utc) - latest_publish).days

            # Check if we already triggered a rebuild within the stale window
            recent_trigger = (
                session.query(StaleBuildTrigger)
                .filter_by(snap_id=snap["id"], status="triggered")
                .filter(StaleBuildTrigger.triggered_at >= cutoff)
                .first()
            )
            if recent_trigger:
                logger.debug(
                    "stale_build_scanner: %s already triggered %s ago, skipping",
                    snap["name"], recent_trigger.triggered_at,
                )
                return "skipped"

        logger.info(
            "stale_build_scanner: %s last published %d days ago — triggering rebuild",
            snap["name"], days_stale,
        )

        from snap_dashboard.github.bot_client import BotGitHubClient
        client = BotGitHubClient(token=token)

        # Ensure the automated build workflow exists in the packaging repo
        default_branch = client.get_default_branch(owner, repo)
        workflow_ready = self._ensure_workflow(client, owner, repo, snap["name"], default_branch)

        if not workflow_ready:
            self._record_trigger(
                snap, stale_days=days_stale,
                status="skipped",
                error="could not create automated-snap-build workflow",
            )
            return "skipped"

        # Dispatch the workflow
        success, err = client.dispatch_workflow(
            owner, repo, _BUILD_WORKFLOW,
            ref=default_branch,
            inputs={"dashboard_trigger_id": str(snap["id"])},
        )

        if success:
            self._record_trigger(snap, stale_days=days_stale, status="triggered")
            logger.info("stale_build_scanner: triggered rebuild for %s", snap["name"])
            return "triggered"
        else:
            self._record_trigger(snap, stale_days=days_stale, status="failed", error=err)
            logger.warning("stale_build_scanner: dispatch failed for %s: %s", snap["name"], err)
            return "error"

    def _ensure_workflow(
        self, client, owner: str, repo: str, snap_name: str, branch: str
    ) -> bool:
        """Create automated-snap-build.yml in the repo if it isn't there yet.

        Returns True if the workflow is ready (already existed or was just created).
        """
        if client.file_exists(owner, repo, WORKFLOW_PATH):
            return True

        logger.info(
            "stale_build_scanner: creating %s in %s/%s",
            WORKFLOW_PATH, owner, repo,
        )
        created = client.create_file(
            owner, repo,
            path=WORKFLOW_PATH,
            content=WORKFLOW_YAML,
            commit_message="ci: add automated snap build workflow (snap-dashboard)",
            branch=branch,
        )
        if not created:
            logger.warning(
                "stale_build_scanner: could not create workflow in %s/%s", owner, repo
            )
        return created

    def _record_trigger(
        self,
        snap: dict,
        stale_days: int,
        status: str,
        error: str | None = None,
    ) -> None:
        with get_session() as session:
            trigger = StaleBuildTrigger(
                snap_id=snap["id"],
                user_id=snap["user_id"],
                packaging_repo=snap["packaging_repo"],
                channel=_BUILD_CHANNEL,
                days_since_publish=stale_days,
                status=status,
                error_msg=error,
            )
            session.add(trigger)
