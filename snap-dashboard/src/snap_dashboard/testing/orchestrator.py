"""YARF test orchestration — find snaps needing tests, trigger workflows, sync status."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import httpx

from snap_dashboard.config import get_config
from snap_dashboard.db.models import ChannelMap, Snap, TestRun
from snap_dashboard.db.session import get_session

logger = logging.getLogger(__name__)

_GH_API = "https://api.github.com"


def _gh_headers(token: str) -> dict[str, str]:
    """Return GitHub API request headers, with auth if a token is provided."""
    h: dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def find_snaps_needing_tests(session) -> list[dict]:
    """Return snaps where candidate or edge version differs from stable, per architecture.

    Returns one entry per snap+channel+architecture combination:
    - candidate entries have ``can_promote=True``  (promotion path to stable)
    - edge entries have ``can_promote=False``       (test-only, no promotion)

    Edge is only included for an arch when its version differs from candidate
    for that same arch (avoids duplicate rows).

    Each dict contains:
      snap          – Snap ORM object
      architecture  – e.g. "amd64", "arm64"
      from_channel  – "candidate" or "edge"
      version       – version string in that channel+arch
      revision      – revision int in that channel+arch (may be None)
      stable_ver    – current stable version for that arch (may be None)
      can_promote   – True for candidate, False for edge
    """
    snaps = session.query(Snap).order_by(Snap.name).all()
    results: list[dict] = []

    for snap in snaps:
        cm_rows = session.query(ChannelMap).filter_by(snap_id=snap.id).all()
        # channels[channel][arch] = {version, revision}
        channels: dict[str, dict[str, dict]] = {}
        for cm in cm_rows:
            channels.setdefault(cm.channel, {})[cm.architecture] = {
                "version": cm.version,
                "revision": cm.revision,
            }

        # Collect all architectures published across any channel
        all_archs: set[str] = set()
        for arch_map in channels.values():
            all_archs.update(arch_map.keys())

        for arch in sorted(all_archs):
            def _get(ch: str, a: str = arch) -> dict:
                return (channels.get(ch) or {}).get(a) or {}

            stable_ver = _get("stable").get("version")
            candidate_info = _get("candidate")
            candidate_ver = candidate_info.get("version")

            # Candidate — promotion path to stable
            if candidate_ver and candidate_ver != stable_ver:
                results.append(
                    {
                        "snap": snap,
                        "architecture": arch,
                        "from_channel": "candidate",
                        "version": candidate_ver,
                        "revision": candidate_info.get("revision"),
                        "stable_ver": stable_ver,
                        "can_promote": True,
                    }
                )

            # Edge — test-only; only when version differs from candidate for this arch
            edge_info = _get("edge")
            edge_ver = edge_info.get("version")
            if edge_ver and edge_ver != stable_ver and edge_ver != candidate_ver:
                results.append(
                    {
                        "snap": snap,
                        "architecture": arch,
                        "from_channel": "edge",
                        "version": edge_ver,
                        "revision": edge_info.get("revision"),
                        "stable_ver": stable_ver,
                        "can_promote": False,
                    }
                )

    return results


def suite_exists_in_repo(testing_repo: str, snap_name: str, token: str) -> bool:
    """Return True if ``suites/{snap_name}/suite/__init__.robot`` exists in the testing repo."""
    if not testing_repo:
        return False
    owner, _, repo = testing_repo.partition("/")
    if not repo:
        return False

    url = (
        f"{_GH_API}/repos/{owner}/{repo}/contents"
        f"/suites/{snap_name}/suite/__init__.robot"
    )
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(url, headers=_gh_headers(token))
            return resp.status_code == 200
    except httpx.RequestError as exc:
        logger.warning("suite_exists_in_repo request failed for %s: %s", snap_name, exc)
        return False


def trigger_workflow(
    snap_name: str,
    from_channel: str,
    version: str,
    revision: int | None,
    architecture: str = "amd64",
    triggered_by: str = "manual",
) -> tuple[bool, str, int | None]:
    """Dispatch a ``workflow_dispatch`` event to run YARF tests for the given snap.

    Creates a :class:`TestRun` record in the database before dispatching.

    Returns:
        A ``(success, error_message, db_run_id)`` tuple.  ``error_message`` is
        an empty string on success; ``db_run_id`` is the new TestRun PK or None
        on failure.
    """
    config = get_config()
    if not config.testing_repo:
        return False, "No testing_repo configured", None
    if not config.github_token:
        return False, "No GitHub token configured", None

    owner, _, repo = config.testing_repo.partition("/")
    if not repo:
        return False, f"Invalid testing_repo format: {config.testing_repo!r} (expected owner/repo)", None

    # Persist a TestRun record first so we have a run_id to pass as an input.
    with get_session() as session:
        run = TestRun(
            snap_name=snap_name,
            architecture=architecture,
            from_channel=from_channel,
            version=version,
            revision=revision,
            status="pending",
            triggered_by=triggered_by,
        )
        session.add(run)
        session.flush()
        run_id = run.id

    url = f"{_GH_API}/repos/{owner}/{repo}/actions/workflows/snap-test.yml/dispatches"
    payload = {
        "ref": "main",
        "inputs": {
            "snap_name": snap_name,
            "from_channel": from_channel,
            "architecture": architecture,
            "version": str(version),
            "revision": str(revision or 0),
            "dashboard_run_id": str(run_id),
        },
    }
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(url, json=payload, headers=_gh_headers(config.github_token))

        if resp.status_code == 204:
            with get_session() as session:
                run = session.query(TestRun).get(run_id)
                if run:
                    run.status = "triggered"
            return True, "", run_id
        else:
            err = f"GitHub API returned {resp.status_code}: {resp.text[:300]}"
            with get_session() as session:
                run = session.query(TestRun).get(run_id)
                if run:
                    run.status = "error"
                    run.error_msg = err
            return False, err, None

    except httpx.RequestError as exc:
        err = str(exc)
        with get_session() as session:
            run = session.query(TestRun).get(run_id)
            if run:
                run.status = "error"
                run.error_msg = err
        return False, err, None


def poll_for_gh_run_id(db_run_id: int, triggered_at: datetime) -> None:
    """Background task: find the GH Actions run for our dispatch then monitor it to completion.

    Phase 1 — find the run ID by polling the Actions API (up to ~3 min).
    Phase 2 — poll the run's status every 30s until it reaches a terminal
               state (passed/failed) or 90 minutes elapse.

    Status updates are written directly to the DB so the JS polling picks
    them up without a manual sync.
    """
    config = get_config()
    if not config.testing_repo or not config.github_token:
        return
    owner, _, repo = config.testing_repo.partition("/")
    if not repo:
        return

    headers = _gh_headers(config.github_token)

    # ---- Phase 1: find the run ID ----------------------------------------
    list_url = f"{_GH_API}/repos/{owner}/{repo}/actions/runs"
    params = {"event": "workflow_dispatch", "per_page": 20}
    gh_run_id: str | None = None

    for _attempt in range(9):  # up to ~3 min (9 × 20s)
        time.sleep(20)
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.get(list_url, params=params, headers=headers)
            if resp.status_code != 200:
                continue
            for run in resp.json().get("workflow_runs", []):
                if "snap-test" not in run.get("path", ""):
                    continue
                created_str = run.get("created_at", "")
                if not created_str:
                    continue
                created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                if created < triggered_at:
                    continue
                gh_run_id = str(run["id"])
                break
        except Exception as exc:
            logger.warning("poll_for_gh_run_id phase 1 attempt failed: %s", exc)

        if gh_run_id:
            with get_session() as session:
                db_run = session.query(TestRun).get(db_run_id)
                if db_run:
                    db_run.gh_run_id = gh_run_id
                    db_run.status = "running"
            logger.info("poll_for_gh_run_id: found run %s for TestRun %s", gh_run_id, db_run_id)
            break
    else:
        logger.warning("poll_for_gh_run_id: gave up finding GH run for TestRun %s", db_run_id)
        return

    # ---- Phase 2: monitor until complete ------------------------------------
    for _attempt in range(180):  # up to 90 min (180 × 30s)
        time.sleep(30)
        new_status = _check_gh_run_status(gh_run_id, owner, repo, config.github_token)
        if new_status is None:
            continue
        with get_session() as session:
            db_run = session.query(TestRun).get(db_run_id)
            if db_run and db_run.status != new_status:
                db_run.status = new_status
                if new_status in ("passed", "failed"):
                    db_run.finished_at = datetime.now(timezone.utc)
        if new_status in ("passed", "failed"):
            logger.info("poll_for_gh_run_id: run %s finished as %s", gh_run_id, new_status)
            return


def _check_gh_run_status(gh_run_id: str, owner: str, repo: str, token: str) -> str | None:
    """Return a dashboard status string for a GH Actions run, or None on error."""
    url = f"{_GH_API}/repos/{owner}/{repo}/actions/runs/{gh_run_id}"
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(url, headers=_gh_headers(token))
        if resp.status_code != 200:
            return None
        data = resp.json()
        gha_status = data.get("status", "")
        conclusion = data.get("conclusion") or ""
        if gha_status == "completed":
            return "passed" if conclusion == "success" else "failed"
        if gha_status == "in_progress":
            return "running"
        return None  # queued / unknown — leave as-is
    except Exception:
        return None


def sync_test_runs() -> None:
    """Poll GitHub for open test PRs and reconcile with local :class:`TestRun` records.

    - Updates existing *pending/triggered/running* runs with PR metadata.
    - Creates new ``TestRun`` records for PRs that arrived without a prior dispatch
      (e.g. runs triggered externally or before the dashboard was set up).
    """
    config = get_config()
    if not config.testing_repo or not config.github_token:
        return

    owner, _, repo = config.testing_repo.partition("/")
    if not repo:
        return

    from snap_dashboard.github.pr_viewer import get_test_prs, parse_pr_metadata

    try:
        prs = get_test_prs(config.testing_repo, config.github_token)
    except Exception as exc:
        logger.warning("sync_test_runs: failed to fetch test PRs: %s", exc)
        return

    # Build a lookup keyed by (snap_name, version) → PR dict
    pr_map: dict[tuple[str, str], dict] = {}
    for pr in prs:
        meta = parse_pr_metadata(pr.get("body", ""))
        snap = meta.get("snap")
        version = meta.get("version")
        if snap and version:
            pr_map[(snap, version)] = pr

    with get_session() as session:
        # Update runs that are still in-flight
        in_flight = session.query(TestRun).filter(
            TestRun.status.in_(["pending", "triggered", "running"])
        ).all()

        for run in in_flight:
            # If we have a GH run ID but no PR yet, check GH Actions API directly
            pr = pr_map.get((run.snap_name, run.version or ""))
            if not pr and run.gh_run_id:
                gha_status = _check_gh_run_status(run.gh_run_id, owner, repo, config.github_token)
                if gha_status and gha_status != run.status:
                    run.status = gha_status
                    if gha_status in ("passed", "failed"):
                        run.finished_at = datetime.now(timezone.utc)
                continue
            if not pr:
                continue
            meta = parse_pr_metadata(pr.get("body", ""))
            gh_status = meta.get("status", "")
            if gh_status == "passed":
                run.status = "passed"
            elif gh_status == "failed":
                run.status = "failed"
            else:
                run.status = "running"

            run.pr_number = pr.get("number")
            run.pr_url = pr.get("html_url")
            run.pr_body = pr.get("body")
            run.gh_run_id = meta.get("gh_run_id") or run.gh_run_id

            if gh_status in ("passed", "failed"):
                run.finished_at = datetime.now(timezone.utc)

        # Create stubs for externally-triggered PRs we have no record for
        known_keys = {
            (r.snap_name, r.version or "")
            for r in session.query(TestRun).all()
        }
        for pr in prs:
            meta = parse_pr_metadata(pr.get("body", ""))
            snap = meta.get("snap")
            version = meta.get("version")
            if not snap or not version:
                continue
            if (snap, version) in known_keys:
                continue

            gh_status = meta.get("status", "running")
            new_run = TestRun(
                snap_name=snap,
                architecture=meta.get("architecture", "amd64"),
                from_channel=meta.get("from_channel", "unknown"),
                version=version,
                revision=int(meta["revision"]) if meta.get("revision", "").isdigit() else None,
                status=gh_status if gh_status in ("passed", "failed") else "running",
                gh_run_id=meta.get("gh_run_id"),
                pr_number=pr.get("number"),
                pr_url=pr.get("html_url"),
                pr_body=pr.get("body"),
                triggered_by="external",
            )
            if new_run.status in ("passed", "failed"):
                new_run.finished_at = datetime.now(timezone.utc)
            session.add(new_run)
