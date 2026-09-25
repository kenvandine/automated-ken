"""Data collector — fetches Snap Store and GitHub/GitLab data into the DB."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from snap_dashboard.config import Config
from snap_dashboard.db.models import ChannelMap, CollectionRun, Issue, Snap
from snap_dashboard.db.session import get_session
from snap_dashboard.github.client import GitHubClient
from snap_dashboard.github.repo_discovery import build_packaging_repo_map
from snap_dashboard.store.client import (
    extract_channel_map,
    extract_repo_urls,
    find_snaps_by_publisher,
    get_snap_info,
)

logger = logging.getLogger(__name__)


def run_collection(config: Config, user_id: int | None = None) -> dict:
    """Run the full data collection pipeline.

    Fetches Snap Store / GitHub / GitLab data for every snap. Network calls
    (get_snap_info, build_packaging_repo_map, get_open_issues_and_prs, and
    the first-run publisher discovery) are made with no DB session/lock
    held — get_session() serializes all sqlite access process-wide, so
    holding it across ~150 snaps worth of blocking HTTP calls would stall
    every other request, agent, and runner heartbeat in the app for the
    full duration of the run.

    Returns a summary dict: {snaps_updated, issues_updated, status, error}.
    """
    started_at = datetime.now(timezone.utc)
    with get_session() as session:
        run = CollectionRun(started_at=started_at, status="running", user_id=user_id)
        session.add(run)
        session.flush()
        run_id = run.id

        q = session.query(Snap)
        if user_id is not None:
            q = q.filter_by(user_id=user_id)
        snap_count = q.count()

    snaps_updated = 0
    issues_updated = 0
    status = "success"
    error_msg: str | None = None

    try:
        # Step 1: Discover snaps on first run
        if snap_count == 0 and config.publisher:
            logger.info("First run — discovering snaps for publisher %r", config.publisher)
            discovered = find_snaps_by_publisher(config.publisher)
            with get_session() as session:
                for s in discovered:
                    if not s.get("name"):
                        continue
                    existing = session.query(Snap).filter_by(
                        name=s["name"], user_id=user_id
                    ).first()
                    if not existing:
                        snap_obj = Snap(
                            name=s["name"],
                            publisher=s.get("publisher", config.publisher),
                            manually_added=False,
                            user_id=user_id,
                        )
                        session.add(snap_obj)
                session.flush()
            logger.info("Discovered %d snaps", len(discovered))

        # Step 2: Read the current snap list (plain dicts, no lock held
        # afterward) then update each snap.
        with get_session() as session:
            q2 = session.query(Snap)
            if user_id is not None:
                q2 = q2.filter_by(user_id=user_id)
            snaps_data = [
                {
                    "id": s.id,
                    "name": s.name,
                    "manually_added": s.manually_added,
                    "packaging_repo": s.packaging_repo,
                    "upstream_repo": s.upstream_repo,
                }
                for s in q2.all()
            ]

        gh_client = GitHubClient(token=config.github_token)
        for snap_data in snaps_data:
            try:
                update = _fetch_snap_update(snap_data, gh_client)
                with get_session() as session:
                    issues_for_snap = _apply_snap_update(session, snap_data["id"], update)
                snaps_updated += 1
                issues_updated += issues_for_snap
            except Exception as exc:
                logger.warning("Error updating snap %r: %s", snap_data["name"], exc)
                status = "partial"

    except Exception as exc:
        logger.error("Collection failed: %s", exc)
        status = "error"
        error_msg = str(exc)

    finished_at = datetime.now(timezone.utc)
    with get_session() as session:
        run = session.query(CollectionRun).get(run_id)
        if run:
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


def collect_one(
    config: Config,
    snap_name: str,
    user_id: int | None = None,
) -> dict:
    """Run collection for a single snap by name.

    Returns a summary dict: {snap, status, error}.
    """
    with get_session() as session:
        q = session.query(Snap).filter_by(name=snap_name)
        if user_id is not None:
            q = q.filter_by(user_id=user_id)
        snap = q.first()
        if not snap:
            return {"snap": snap_name, "status": "error", "error": "Snap not found"}
        snap_data = {
            "id": snap.id,
            "name": snap.name,
            "manually_added": snap.manually_added,
            "packaging_repo": snap.packaging_repo,
            "upstream_repo": snap.upstream_repo,
        }

    gh_client = GitHubClient(token=config.github_token)
    try:
        update = _fetch_snap_update(snap_data, gh_client)
        with get_session() as session:
            _apply_snap_update(session, snap_data["id"], update)
        return {"snap": snap_name, "status": "success", "error": None}
    except Exception as exc:
        logger.warning("Error collecting snap %r: %s", snap_name, exc)
        return {"snap": snap_name, "status": "error", "error": str(exc)}


def refresh_channel_map(snap_id: int, snap_name: str) -> bool:
    """Re-fetch just one snap's Store channel map (no GitHub calls).

    Used by the version-bump pipeline to notice a newly published or
    released revision within minutes rather than waiting for the next full
    collection. Returns False if the Store lookup failed.
    """
    info = get_snap_info(snap_name)
    if not info:
        return False
    entries = extract_channel_map(info)
    with get_session() as session:
        snap = session.query(Snap).get(snap_id)
        if snap is None:
            return False
        _apply_snap_update(
            session,
            snap_id,
            {
                "packaging_repo": snap.packaging_repo,
                "upstream_repo": snap.upstream_repo,
                "channel_entries": entries,
                "issues_by_repo": {},
            },
        )
    return True


def _fetch_snap_update(snap_data: dict, gh_client: GitHubClient) -> dict:
    """Fetch all external (Store/GitHub/GitLab) data needed to update one
    snap. Purely network I/O — no DB session is opened here.
    """
    logger.info("Updating snap %r", snap_data["name"])

    packaging_repo = snap_data["packaging_repo"]
    upstream_repo = snap_data["upstream_repo"]
    channel_entries: list[dict] | None = None

    # 2a: Fetch channel map
    info = get_snap_info(snap_data["name"])
    if info:
        # Always re-derive repo URLs from Store metadata for auto-discovered snaps
        # so stale or incorrect values are corrected on each collection run.
        # For manually-added snaps the user's values are preserved.
        if not snap_data["manually_added"]:
            repos = extract_repo_urls(info)
            packaging_repo = repos.get("packaging_repo") or packaging_repo
            upstream_repo = repos.get("upstream_repo") or None

        channel_entries = extract_channel_map(info)

    # Store metadata frequently has no links at all for personal snaps
    # (kenvandine's snaps rarely bother filling in issues/contact/source
    # links). Fall back to scanning the user's own GitHub repos for a
    # snapcraft.yaml declaring this snap's name.
    if not packaging_repo and gh_client.token:
        repo_map = build_packaging_repo_map(gh_client.token)
        discovered = repo_map.get(snap_data["name"])
        if discovered:
            packaging_repo = discovered

    # 2b & 2c: Fetch issues/PRs
    repos_to_fetch: list[str] = []
    if packaging_repo:
        repos_to_fetch.append(packaging_repo)
    if upstream_repo and upstream_repo != packaging_repo:
        repos_to_fetch.append(upstream_repo)

    issues_by_repo: dict[str, list[dict]] = {}
    for repo_url in repos_to_fetch:
        issues_by_repo[repo_url] = gh_client.get_open_issues_and_prs(repo_url)

    return {
        "packaging_repo": packaging_repo,
        "upstream_repo": upstream_repo,
        "channel_entries": channel_entries,
        "issues_by_repo": issues_by_repo,
    }


def _apply_snap_update(session: Session, snap_id: int, update: dict) -> int:
    """Write a previously-fetched update to the DB.

    Returns the number of issues/PRs now on record for this snap.
    """
    snap = session.query(Snap).get(snap_id)
    if not snap:
        return 0

    snap.packaging_repo = update["packaging_repo"]
    snap.upstream_repo = update["upstream_repo"]

    if update["channel_entries"] is not None:
        session.query(ChannelMap).filter_by(snap_id=snap.id).delete()
        for entry in update["channel_entries"]:
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

    if update["issues_by_repo"]:
        session.query(Issue).filter_by(snap_id=snap.id).delete()
        session.flush()
        for repo_url, items in update["issues_by_repo"].items():
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

    return session.query(Issue).filter_by(snap_id=snap.id).count()
