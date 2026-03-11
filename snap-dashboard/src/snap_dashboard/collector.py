"""Data collector — fetches Snap Store and GitHub/GitLab data into the DB."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from snap_dashboard.config import Config
from snap_dashboard.db.models import ChannelMap, CollectionRun, Issue, Snap
from snap_dashboard.github.client import GitHubClient
from snap_dashboard.store.client import (
    extract_channel_map,
    extract_repo_urls,
    find_snaps_by_publisher,
    get_snap_info,
)

logger = logging.getLogger(__name__)


def run_collection(session: Session, config: Config) -> dict:
    """Run the full data collection pipeline.

    Returns a summary dict: {snaps_updated, issues_updated, status, error}.
    """
    started_at = datetime.now(timezone.utc)
    run = CollectionRun(started_at=started_at, status="running")
    session.add(run)
    session.flush()

    snaps_updated = 0
    issues_updated = 0
    status = "success"
    error_msg: str | None = None

    try:
        # Step 1: Discover snaps on first run
        snap_count = session.query(Snap).count()
        if snap_count == 0 and config.publisher:
            logger.info("First run — discovering snaps for publisher %r", config.publisher)
            discovered = find_snaps_by_publisher(config.publisher)
            for s in discovered:
                if not s.get("name"):
                    continue
                existing = session.query(Snap).filter_by(name=s["name"]).first()
                if not existing:
                    snap_obj = Snap(
                        name=s["name"],
                        publisher=s.get("publisher", config.publisher),
                        manually_added=False,
                    )
                    session.add(snap_obj)
            session.flush()
            logger.info("Discovered %d snaps", len(discovered))

        # Step 2: Update each snap
        gh_client = GitHubClient(token=config.github_token)
        all_snaps = session.query(Snap).all()

        for snap in all_snaps:
            try:
                _update_snap(session, snap, gh_client)
                snaps_updated += 1
                issues_for_snap = (
                    session.query(Issue).filter_by(snap_id=snap.id).count()
                )
                issues_updated += issues_for_snap
            except Exception as exc:
                logger.warning("Error updating snap %r: %s", snap.name, exc)
                status = "partial"

    except Exception as exc:
        logger.error("Collection failed: %s", exc)
        status = "error"
        error_msg = str(exc)

    finished_at = datetime.now(timezone.utc)
    run.finished_at = finished_at
    run.status = status
    run.error_msg = error_msg
    session.flush()

    return {
        "snaps_updated": snaps_updated,
        "issues_updated": issues_updated,
        "status": status,
        "error": error_msg,
    }


def _update_snap(session: Session, snap: Snap, gh_client: GitHubClient) -> None:
    """Update channel map and issues for a single snap."""
    logger.info("Updating snap %r", snap.name)

    # 2a: Fetch and upsert channel map
    info = get_snap_info(snap.name)
    if info:
        # Detect repo URLs if not already set
        if not snap.packaging_repo:
            repos = extract_repo_urls(info)
            if repos.get("packaging_repo"):
                snap.packaging_repo = repos["packaging_repo"]
            if repos.get("upstream_repo"):
                snap.upstream_repo = repos["upstream_repo"]

        channel_entries = extract_channel_map(info)
        # Delete all existing channel_map rows for this snap
        session.query(ChannelMap).filter_by(snap_id=snap.id).delete()
        for entry in channel_entries:
            cm = ChannelMap(
                snap_id=snap.id,
                channel=entry["channel"],
                architecture=entry["architecture"],
                revision=entry.get("revision"),
                version=entry.get("version"),
                released_at=entry.get("released_at"),
            )
            session.add(cm)
        session.flush()

    # 2b & 2c: Fetch issues/PRs
    repos_to_fetch: list[tuple[str, str]] = []
    if snap.packaging_repo:
        repos_to_fetch.append((snap.packaging_repo, snap.packaging_repo))
    if snap.upstream_repo and snap.upstream_repo != snap.packaging_repo:
        repos_to_fetch.append((snap.upstream_repo, snap.upstream_repo))

    # Clear existing issues for this snap before refetching
    if repos_to_fetch:
        session.query(Issue).filter_by(snap_id=snap.id).delete()
        session.flush()

    for repo_url, _ in repos_to_fetch:
        items = gh_client.get_open_issues_and_prs(repo_url)
        for item in items:
            issue = Issue(
                snap_id=snap.id,
                repo_url=repo_url,
                issue_number=item["issue_number"],
                title=item["title"],
                state=item["state"],
                type=item["type"],
                url=item["url"],
                author=item["author"],
                created_at=item["created_at"],
                updated_at=item["updated_at"],
            )
            session.add(issue)
        session.flush()
