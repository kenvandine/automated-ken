"""Fleet-level read models shared by the Snaps list and the Overview page.

Everything here is batched (a fixed number of queries regardless of fleet
size) — the old dashboard issued several queries per snap.
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import func, or_

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


def bumps_needing_you_count(session, user_id: int) -> int:
    """Open version-bump PRs waiting on a human (Releases tab badge)."""
    return (
        session.query(func.count(VersionBumpPR.id))
        .filter(VersionBumpPR.user_id == user_id, VersionBumpPR.status.in_(BUMP_NEEDS_YOU))
        .scalar()
        or 0
    )


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
    )
    latest_runs = {
        r.snap_name: r
        for r in session.query(TestRun).filter(TestRun.id.in_(latest_run_ids.subquery().select())).all()
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
    )
    triggers = {
        t.snap_id: t
        for t in session.query(StaleBuildTrigger).filter(StaleBuildTrigger.id.in_(latest_trigger_ids.subquery().select())).all()
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


# ---------------------------------------------------------------------------
# Overview (the "/" page): what needs you, what's in flight, what shipped.
# ---------------------------------------------------------------------------

_RUN_ACTIVE = ("pending", "triggered", "running", "reviewing")
_TASK_ACTIVE = ("queued", "in_progress")
_TASK_NEEDS_YOU = ("waiting_for_user", "failed", "timed_out", "dispatch_failed")
_BUMP_IN_FLIGHT = (
    "dispatched", "open", "ci_pending", "ci_passed", "yarf_running", "yarf_passed",
    "merged", "awaiting_release", "candidate_testing",
)


def build_overview(session, user_id: int, rows: list[dict] | None = None) -> dict:
    """Everything the Overview page shows, as plain dicts."""
    from datetime import datetime, timedelta, timezone

    from snap_dashboard.db.models import AgentRun, CopilotTask, PromotionDismissal, Runner
    from snap_dashboard.runners import effective_status
    from snap_dashboard.testing.release_set import READY
    from snap_dashboard.web.routes.testing import _build_pending_promotion

    if rows is None:
        rows = build_snap_rows(session, user_id)
    tracked = {r["name"] for r in rows}
    now = datetime.now(timezone.utc)
    week_ago = (now - timedelta(days=7)).replace(tzinfo=None)
    needs: list[dict] = []

    # 1. Candidate sets ready to promote to stable.
    candidate_runs = (
        session.query(TestRun)
        .filter(
            TestRun.user_id == user_id,
            TestRun.from_channel == "candidate",
            TestRun.status == "passed",
            TestRun.promoted.is_(False),
        )
        .order_by(TestRun.started_at.desc())
        .limit(100)
        .all()
    )
    runs_data = [
        {
            "snap_name": r.snap_name, "version": r.version, "status": r.status,
            "promoted": r.promoted, "from_channel": r.from_channel,
        }
        for r in candidate_runs
    ]
    dismissed = {
        (d.snap_name, d.version)
        for d in session.query(PromotionDismissal).filter_by(user_id=user_id).all()
    }
    for card in _build_pending_promotion(session, user_id, runs_data, tracked, dismissed):
        ready = [m["architecture"] for m in card["members"] if m["state"] == READY]
        if not ready:
            continue
        needs.append({
            "kind": "promotion",
            "tone": "positive",
            "label": "Ready to promote",
            "title": f"{card['snap_name']} {card['version']}",
            "meta": "Tested on " + ", ".join(ready) + " — promote candidate to stable",
            "href": "/releases#promote",
            "action": "Review & promote",
            "snap": card["snap_name"],
        })

    # 2. Version-bump PRs waiting on a human.
    snap_names = {s.id: s.name for s in session.query(Snap.id, Snap.name).filter_by(user_id=user_id)}
    bumps = (
        session.query(VersionBumpPR)
        .filter(VersionBumpPR.snap_id.in_(list(snap_names) or [-1]))
        .order_by(VersionBumpPR.updated_at.desc())
        .all()
    )
    in_flight_bumps = []
    shipped_bumps = []
    for b in bumps:
        name = snap_names.get(b.snap_id, "?")
        info = {
            "title": f"{name} → {b.new_version or '?'}",
            "status": b.status,
            "href": b.bot_pr_url or "/releases/bumps",
            "snap": name,
            "when": b.updated_at,
        }
        if b.status in BUMP_NEEDS_YOU:
            needs.append({
                "kind": "bump",
                "tone": "negative" if b.status in ("ci_failed", "yarf_failed", "agent_rejected") else "caution",
                "label": "Version bump",
                "title": info["title"],
                "meta": (b.agent_reasoning or "").strip()[:160] or None,
                "status": b.status,
                "href": "/releases/bumps",
                "external": b.bot_pr_url,
                "action": "Review",
                "snap": name,
            })
        elif b.status in _BUMP_IN_FLIGHT:
            in_flight_bumps.append(info)
        elif b.status in ("stable_promoted", "stable_promoted_partial") and b.updated_at and b.updated_at >= week_ago:
            shipped_bumps.append(info)

    # 3. Latest test run per snap failed / needs a manual review.
    for r in rows:
        run = r["latest_run"]
        if run is None or run["promoted"]:
            continue
        if run["status"] in ("failed", "error"):
            needs.append({
                "kind": "test", "tone": "negative", "label": "Test failed",
                "title": f"{r['name']} {run['version'] or ''}".strip(),
                "meta": f"{run['architecture'] or 'amd64'} smoke test {run['status']}",
                "href": f"/testing/runs/{run['id']}", "action": "Investigate", "snap": r["name"],
            })
        elif run["review_decision"] == "needs_review":
            needs.append({
                "kind": "test", "tone": "caution", "label": "Screenshot review",
                "title": f"{r['name']} {run['version'] or ''}".strip(),
                "meta": "The AI reviewer wasn't sure — take a look at the screenshots",
                "href": f"/testing/runs/{run['id']}", "action": "Review", "snap": r["name"],
            })

    # 4. Coding-agent tasks stuck on you (or failed).
    tasks = (
        session.query(CopilotTask)
        .filter(CopilotTask.user_id == user_id)
        .filter(
            or_(
                CopilotTask.status.in_(_TASK_ACTIVE + _TASK_NEEDS_YOU),
                CopilotTask.ci_status == "ci_failed",
            )
        )
        .order_by(CopilotTask.updated_at.desc())
        .limit(50)
        .all()
    )
    active_tasks = []
    for t in tasks:
        if t.status in _TASK_NEEDS_YOU or t.ci_status == "ci_failed":
            if t.status != "waiting_for_user" and (t.updated_at is None or t.updated_at < week_ago):
                continue
            needs.append({
                "kind": "task",
                "tone": "caution" if t.status == "waiting_for_user" else "negative",
                "label": "Coding task",
                "title": f"{t.kind.replace('_', ' ')} · {t.owner_repo}",
                "meta": (t.error_msg or "").strip()[:160] or None,
                "status": "ci_failed" if t.ci_status == "ci_failed" and t.status not in _TASK_NEEDS_YOU else t.status,
                "href": "/agents/tasks",
                "external": t.pr_url,
                "action": "Open",
            })
        else:
            active_tasks.append({
                "title": f"{t.kind.replace('_', ' ')} · {t.owner_repo}",
                "status": t.status,
                "href": t.pr_url or "/agents/tasks",
                "when": t.created_at,
            })

    # 5. Runners that dropped off.
    runners = session.query(Runner).filter(Runner.user_id == user_id, Runner.revoked_at.is_(None)).all()
    runner_states = [effective_status(r) for r in runners]
    for r, st in zip(runners, runner_states):
        if st == "offline":
            needs.append({
                "kind": "runner", "tone": "caution", "label": "Runner offline",
                "title": r.name, "meta": f"{r.arch or ''} — test runs for this architecture will queue",
                "href": "/runners", "action": "Check",
            })

    # 6. Failed rebuilds.
    for r in rows:
        if "Rebuild failed" in r["attention"]:
            needs.append({
                "kind": "rebuild", "tone": "negative", "label": "Rebuild failed",
                "title": r["name"], "meta": None,
                "href": f"/snap/{r['name']}", "action": "Open", "snap": r["name"],
            })

    # In flight --------------------------------------------------------
    running_agents = [
        {"agent_type": a.agent_type, "snap": a.snap_name, "when": a.started_at, "id": a.id}
        for a in session.query(AgentRun)
        .filter(AgentRun.user_id == user_id, AgentRun.status == "running")
        .order_by(AgentRun.started_at.desc())
        .limit(20)
    ]
    active_runs = [
        {"id": t.id, "snap": t.snap_name, "version": t.version, "arch": t.architecture or "amd64",
         "status": t.status, "when": t.started_at}
        for t in session.query(TestRun)
        .filter(TestRun.user_id == user_id, TestRun.status.in_(_RUN_ACTIVE))
        .order_by(TestRun.started_at.desc())
        .limit(20)
    ]

    # Recently shipped -------------------------------------------------
    promoted = {}
    for t in (
        session.query(TestRun)
        .filter(TestRun.user_id == user_id, TestRun.promoted.is_(True), TestRun.promoted_at >= week_ago)
        .order_by(TestRun.promoted_at.desc())
        .limit(50)
    ):
        key = (t.snap_name, t.version)
        entry = promoted.setdefault(key, {"snap": t.snap_name, "version": t.version, "arches": [], "when": t.promoted_at})
        entry["arches"].append(t.architecture or "amd64")
    shipped = list(promoted.values())[:10]

    online = sum(1 for s in runner_states if s in ("idle", "busy", "locked"))
    return {
        "needs": needs,
        "running_agents": running_agents,
        "active_runs": active_runs,
        "in_flight_bumps": in_flight_bumps[:10],
        "active_tasks": active_tasks[:10],
        "shipped": shipped,
        "shipped_bumps": shipped_bumps[:10],
        "stats": {
            "snaps": len(rows),
            "needs": len(needs),
            "in_flight": len(running_agents) + len(active_runs) + len(in_flight_bumps) + len(active_tasks),
            "runners_online": online,
            "runners_total": len(runners),
            "shipped": len(shipped),
        },
    }
