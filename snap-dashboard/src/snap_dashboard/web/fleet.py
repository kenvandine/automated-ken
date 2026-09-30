"""Fleet-level read models shared by the Snaps list and the Overview page.

Everything here is batched (a fixed number of queries regardless of fleet
size) — the old dashboard issued several queries per snap.
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import func

from snap_dashboard.db.models import (
    ChannelMap,
    Issue,
    Snap,
    StaleBuildTrigger,
    TestRun,
    VersionBumpPR,
)

# Version-bump statuses where the PR is still moving through the pipeline.
BUMP_ACTIVE = (
    "open", "ci_pending", "ci_passed", "ci_failed", "yarf_running", "yarf_passed",
    "yarf_failed", "needs_review", "agent_approved", "dispatched", "merged",
    "awaiting_release", "candidate_testing",
)
# Of those, the ones that are waiting on a human.
BUMP_NEEDS_YOU = ("agent_approved", "needs_review", "ci_failed", "yarf_failed", "agent_rejected")

CHANNEL_ORDER = ("stable", "candidate", "beta", "edge")


def snap_type(snap) -> str:
    if getattr(snap, "is_service", False):
        return "service"
    if getattr(snap, "is_console_app", False):
        return "console"
    return "desktop"


def _pick(by_arch: dict[str, str | None]) -> str | None:
    if not by_arch:
        return None
    if by_arch.get("amd64"):
        return by_arch["amd64"]
    for arch in sorted(by_arch):
        if by_arch[arch]:
            return by_arch[arch]
    return None


def build_snap_rows(session, user_id: int) -> list[dict]:
    """One dict per tracked snap with everything the Snaps table needs."""
    snaps = session.query(Snap).filter_by(user_id=user_id).order_by(Snap.name).all()
    if not snaps:
        return []
    ids = [s.id for s in snaps]
    names = [s.name for s in snaps]

    channels: dict[int, dict[str, dict[str, str | None]]] = defaultdict(lambda: defaultdict(dict))
    last_collected: dict[int, object] = {}
    for cm in session.query(ChannelMap).filter(ChannelMap.snap_id.in_(ids)).all():
        channels[cm.snap_id][cm.channel][cm.architecture] = cm.version
        if cm.fetched_at and (cm.snap_id not in last_collected or cm.fetched_at > last_collected[cm.snap_id]):
            last_collected[cm.snap_id] = cm.fetched_at

    counts: dict[tuple[int, str], int] = {}
    for snap_id, typ, n in (
        session.query(Issue.snap_id, Issue.type, func.count(Issue.id))
        .filter(Issue.snap_id.in_(ids), Issue.state == "open")
        .group_by(Issue.snap_id, Issue.type)
        .all()
    ):
        counts[(snap_id, typ)] = n

    latest_run_ids = (
        session.query(func.max(TestRun.id))
        .filter(TestRun.user_id == user_id, TestRun.snap_name.in_(names))
        .group_by(TestRun.snap_name)
        .subquery()
    )
    latest_runs = {
        r.snap_name: r
        for r in session.query(TestRun).filter(TestRun.id.in_(latest_run_ids)).all()
    }

    bumps: dict[int, VersionBumpPR] = {}
    for b in (
        session.query(VersionBumpPR)
        .filter(VersionBumpPR.snap_id.in_(ids), VersionBumpPR.status.in_(BUMP_ACTIVE + BUMP_NEEDS_YOU))
        .order_by(VersionBumpPR.created_at.desc())
        .all()
    ):
        bumps.setdefault(b.snap_id, b)

    latest_trigger_ids = (
        session.query(func.max(StaleBuildTrigger.id))
        .filter(StaleBuildTrigger.snap_id.in_(ids))
        .group_by(StaleBuildTrigger.snap_id)
        .subquery()
    )
    triggers = {
        t.snap_id: t
        for t in session.query(StaleBuildTrigger).filter(StaleBuildTrigger.id.in_(latest_trigger_ids)).all()
    }

    rows = []
    for s in snaps:
        ch = channels.get(s.id, {})
        stable_by_arch = dict(ch.get("stable", {}))
        arches = sorted({a for per in ch.values() for a in per})
        versions = {c: _pick(ch.get(c, {})) for c in CHANNEL_ORDER}
        stable = versions["stable"]
        ahead = None
        for c in ("candidate", "beta", "edge"):
            if versions[c] and versions[c] != stable:
                ahead = {"channel": c, "version": versions[c]}
                break

        run = latest_runs.get(s.name)
        bump = bumps.get(s.id)
        trig = triggers.get(s.id)

        attention: list[str] = []
        if not s.packaging_repo:
            attention.append("No packaging repo")
        if ch and not stable:
            attention.append("Not in stable")
        if run is not None and not run.promoted and run.status in ("failed", "error"):
            attention.append("Tests failed")
        if run is not None and run.review_decision == "needs_review" and not run.promoted:
            attention.append("Review needed")
        if bump is not None and bump.status in BUMP_NEEDS_YOU:
            attention.append("Bump PR needs you")
        if trig is not None and trig.status == "failed":
            attention.append("Rebuild failed")

        rows.append(
            {
                "name": s.name,
                "publisher": s.publisher,
                "type": snap_type(s),
                "manually_added": s.manually_added,
                "packaging_repo": s.packaging_repo,
                "upstream_repo": s.upstream_repo,
                "arches": arches,
                "stable_by_arch": stable_by_arch,
                "versions": versions,
                "stable": stable,
                "ahead": ahead,
                "issue_count": counts.get((s.id, "issue"), 0),
                "pr_count": counts.get((s.id, "pr"), 0),
                "last_collected": last_collected.get(s.id),
                "latest_run": (
                    {
                        "id": run.id,
                        "status": run.status,
                        "review_decision": run.review_decision,
                        "promoted": run.promoted,
                        "version": run.version,
                        "architecture": run.architecture,
                    }
                    if run is not None else None
                ),
                "bump": (
                    {
                        "id": bump.id,
                        "status": bump.status,
                        "new_version": bump.new_version,
                        "pr_url": bump.bot_pr_url,
                    }
                    if bump is not None else None
                ),
                "attention": attention,
            }
        )
    return rows
