"""Candidate release sets — every testable architecture of one version, promoted together.

A snap version in the candidate channel is usually built for several
architectures, each with its own Store revision and its own runner test.
Promoting them one at a time as each test finishes would put stable into
a mixed state (amd64 on the new version, arm64 still on the old one), so
the auto-promoter and the manual "Promote set" actions both work on the
whole set: every architecture must be ready — or be explicitly skipped by
a confirmed manual override — before any of them is released.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from snap_dashboard.db.models import ChannelMap, Snap, TestRun, VersionBumpPR
from snap_dashboard.db.session import get_session
from snap_dashboard.testing.orchestrator import TESTABLE_ARCHITECTURES

logger = logging.getLogger(__name__)

READY = "ready"
PROMOTED = "promoted"


def candidate_release_set(session, user_id: int, snap_name: str, version: str) -> list[dict]:
    """Return one entry per testable architecture of *snap_name* candidate *version*.

    Members are the testable architectures the Store's candidate channel
    has at this version (from ``ChannelMap``), plus any that have a
    candidate test run for it (in case the channel has since moved on).
    Each entry: ``architecture``, ``run`` (latest candidate ``TestRun`` for
    that architecture and version, or None), ``revision`` and ``promoted``.
    """
    revisions: dict[str, int | None] = {}
    snap = session.query(Snap).filter_by(name=snap_name, user_id=user_id).first()
    if snap:
        for cm in session.query(ChannelMap).filter_by(snap_id=snap.id, channel="candidate", version=version):
            if cm.architecture in TESTABLE_ARCHITECTURES:
                revisions[cm.architecture] = cm.revision

    latest: dict[str, TestRun] = {}
    promoted: set[str] = set()
    runs = (
        session.query(TestRun)
        .filter_by(user_id=user_id, snap_name=snap_name, version=version, from_channel="candidate")
        .order_by(TestRun.id.asc())
        .all()
    )
    for run in runs:
        arch = run.architecture or "amd64"
        if arch not in TESTABLE_ARCHITECTURES:
            continue
        latest[arch] = run
        if run.promoted:
            promoted.add(arch)

    members = []
    for arch in TESTABLE_ARCHITECTURES:
        if arch not in revisions and arch not in latest:
            continue
        run = latest.get(arch)
        revision = run.revision if run and run.revision is not None else revisions.get(arch)
        members.append(
            {"architecture": arch, "run": run, "revision": revision, "promoted": arch in promoted}
        )
    return members


def member_state(member: dict, auto_threshold: float | None = None) -> str:
    """Return ``"promoted"``, ``"ready"``, or a short reason the member isn't ready.

    With ``auto_threshold`` set (auto-promotion), a member additionally
    needs an "approve" review at or above that confidence.
    """
    if member["promoted"]:
        return PROMOTED
    run = member["run"]
    if run is None:
        return "not tested"
    if run.status != "passed":
        return run.status
    if member["revision"] is None:
        return "no revision"
    if auto_threshold is not None:
        if run.review_decision != "approve":
            return f"review: {run.review_decision or 'pending'}"
        if (run.review_confidence or 0.0) < auto_threshold:
            return "review confidence below threshold"
    return READY


def describe(member: dict, state: str) -> str:
    return f"{member['architecture']} ({state})"


def promote_release_set(
    user_id: int,
    snap_name: str,
    version: str,
    run_ids: list[int],
    uc,
    skipped: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Promote the given runs' revisions to stable as one set.

    The runs are first claimed (status ``promoting``) in a single locked
    session, so two callers racing on the same set — e.g. the amd64 and
    arm64 auto-promoters finishing together — can't both release it.
    ``skipped`` lists architectures deliberately left out by a manual
    override; it's recorded on each promoted run so the override is never
    mistaken for a normal full-set promotion.

    Returns ``(promoted_architectures, failure_messages)``.
    """
    from snap_dashboard.testing.baselines import persist_stable_baseline_for_run
    from snap_dashboard.testing.promoter import promote_snap

    claimed: list[dict] = []
    with get_session() as session:
        runs = [r for r in (session.query(TestRun).get(rid) for rid in run_ids) if r]
        if any(r.status == "promoting" for r in runs):
            return [], ["a promotion of this release set is already in progress"]
        for run in runs:
            if run.promoted or run.status != "passed" or run.user_id != user_id:
                continue
            revision = run.revision
            if revision is None:
                snap = session.query(Snap).filter_by(name=snap_name, user_id=user_id).first()
                cm = (
                    session.query(ChannelMap)
                    .filter_by(snap_id=snap.id, channel="candidate",
                               architecture=run.architecture or "amd64", version=version)
                    .first()
                    if snap else None
                )
                revision = cm.revision if cm else None
            if revision is None:
                continue
            run.revision = revision
            run.status = "promoting"
            claimed.append(
                {"id": run.id, "arch": run.architecture or "amd64", "revision": revision, "repo": run.repo or ""}
            )

    if not claimed:
        return [], ["nothing in this release set is ready to promote"]

    credentials = getattr(uc, "snapcraft_macaroon", "") or ""
    token = getattr(uc, "github_token", "") or ""
    note = ""
    if skipped:
        note = f"Promoted by manual override without: {', '.join(skipped)}."

    promoted_archs: list[str] = []
    failures: list[str] = []
    for item in claimed:
        ok, output = promote_snap(snap_name, item["revision"], "stable", store_credentials=credentials)
        with get_session() as session:
            run = session.query(TestRun).get(item["id"])
            if run is None:
                continue
            if ok:
                run.status = "promoted"
                run.promoted = True
                run.promoted_at = datetime.now(timezone.utc)
                run.error_msg = note or None
                promoted_archs.append(item["arch"])
            else:
                run.status = "passed"
                run.error_msg = f"Promotion failed: {output[:400]}"
                failures.append(f"{item['arch']} rev {item['revision']}: {output[:200]}")

    for item in claimed:
        if item["arch"] in promoted_archs:
            persist_stable_baseline_for_run(item["id"], item["repo"] or getattr(uc, "testing_repo", ""), token)

    promoted_ids = [i["id"] for i in claimed if i["arch"] in promoted_archs]
    if promoted_ids:
        with get_session() as session:
            for bump in session.query(VersionBumpPR).filter(VersionBumpPR.test_run_id.in_(promoted_ids)):
                bump.status = "stable_promoted_partial" if skipped else "stable_promoted"

    logger.info(
        "release set %s %s: promoted %s%s%s",
        snap_name, version, ", ".join(promoted_archs) or "nothing",
        f"; skipped {', '.join(skipped)}" if skipped else "",
        f"; failed {'; '.join(failures)}" if failures else "",
    )
    return promoted_archs, failures
