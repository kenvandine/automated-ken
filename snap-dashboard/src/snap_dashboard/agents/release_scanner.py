"""Release scanner agent — checks packaging repos for new upstream versions."""

from __future__ import annotations

import logging

from snap_dashboard.agents.base import BaseAgent
from snap_dashboard.auth import get_user_config
from snap_dashboard.db.models import Snap, UpstreamRelease
from snap_dashboard.db.session import get_session
from snap_dashboard.snapcraft.fetcher import fetch_snapcraft_yaml
from snap_dashboard.snapcraft.parser import parse_snapcraft_yaml
from snap_dashboard.snapcraft.upstream import get_latest_version, is_newer

logger = logging.getLogger(__name__)


class ReleaseScannerAgent(BaseAgent):
    """Scans all snaps' packaging repos for new upstream releases.

    When a newer version is found, an UpstreamRelease record is created and
    a VersionBumperAgent is spawned for that snap.
    """

    agent_type = "release_scanner"

    def __init__(self, user_id: int | None = None) -> None:
        super().__init__(user_id=user_id)

    def _run(self) -> str:
        uc = get_user_config(self.user_id) if self.user_id else None
        token = (uc.github_token if uc else "") or ""

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

        self._report(f"Scanning {len(snaps)} packaging repos…")
        found = 0
        for snap in snaps:
            try:
                self._report(f"Fetching snapcraft.yaml for {snap['name']}", snap["name"])
                discovered = self._scan_snap(snap, token)
                found += discovered
            except Exception as exc:
                logger.warning("release_scanner: error scanning %s: %s", snap["name"], exc)

        return f"scanned {len(snaps)} snaps, found {found} new upstream release(s)"

    def _scan_snap(self, snap: dict, token: str) -> int:
        """Scan one snap; return number of new releases discovered."""
        yaml_text = fetch_snapcraft_yaml(snap["packaging_repo"], token)
        if not yaml_text:
            return 0

        parts = parse_snapcraft_yaml(yaml_text)
        found = 0

        for part in parts:
            if not part.source or part.source_type == "local":
                continue
            self._report(f"Checking upstream {part.source_type} for {snap['name']}/{part.part_name}", snap["name"])
            info = get_latest_version(
                source=part.source,
                source_type=part.source_type,
                current_version=part.current_version,
                token=token,
            )
            if not info:
                continue

            latest = info.latest_version
            current = part.current_version

            if not is_newer(latest, current):
                continue

            # Check we haven't already recorded this release
            with get_session() as session:
                existing = (
                    session.query(UpstreamRelease)
                    .filter_by(
                        snap_id=snap["id"],
                        part_name=part.part_name,
                        latest_version=latest,
                    )
                    .first()
                )
                if existing:
                    continue

                release = UpstreamRelease(
                    snap_id=snap["id"],
                    part_name=part.part_name,
                    source_type=part.source_type,
                    source_url=part.source,
                    current_version=current,
                    latest_version=latest,
                    release_url=info.release_url,
                    release_notes=info.release_notes,
                    acted_on=False,
                )
                session.add(release)
                session.flush()
                release_id = release.id

            logger.info(
                "release_scanner: new release for %s/%s: %s → %s",
                snap["name"], part.part_name, current, latest,
            )
            found += 1

            # Spawn a version bumper for this release
            self._spawn_bumper(snap, release_id, part, latest, info)

        return found

    def _spawn_bumper(self, snap: dict, release_id: int, part, latest: str, info) -> None:
        from snap_dashboard.agents.version_bumper import VersionBumperAgent
        from snap_dashboard.agents.runner import get_runner

        bumper = VersionBumperAgent(
            snap_id=snap["id"],
            snap_name=snap["name"],
            packaging_repo=snap["packaging_repo"],
            upstream_release_id=release_id,
            part_name=part.part_name,
            old_version=part.current_version,
            new_version=latest,
            release_url=info.release_url,
            release_notes=info.release_notes or "",
            user_id=snap["user_id"],
        )
        get_runner().submit(bumper)
