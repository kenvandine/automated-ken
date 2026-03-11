"""YARF test orchestration — find snaps needing tests, trigger workflows, sync status."""

from __future__ import annotations

import logging
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
    """Return snaps where candidate/beta/edge version differs from stable (amd64).

    Each dict in the returned list contains:
      snap          – Snap ORM object
      from_channel  – highest-priority channel that is ahead ("candidate" > "beta" > "edge")
      version       – version string in that channel
      revision      – revision int in that channel (may be None)
      stable_ver    – current stable version (may be None if not yet released)
    """
    snaps = session.query(Snap).order_by(Snap.name).all()
    results: list[dict] = []

    for snap in snaps:
        cm_rows = session.query(ChannelMap).filter_by(snap_id=snap.id).all()
        channels: dict[str, dict[str, dict]] = {}
        for cm in cm_rows:
            if cm.channel not in channels:
                channels[cm.channel] = {}
            channels[cm.channel][cm.architecture] = {
                "version": cm.version,
                "revision": cm.revision,
            }

        def _get_amd64(ch: str) -> dict:
            return (channels.get(ch) or {}).get("amd64") or {}

        stable = _get_amd64("stable")
        stable_ver = stable.get("version")

        for ch in ("candidate", "beta", "edge"):
            info = _get_amd64(ch)
            ver = info.get("version")
            rev = info.get("revision")
            if ver and ver != stable_ver:
                results.append(
                    {
                        "snap": snap,
                        "from_channel": ch,
                        "version": ver,
                        "revision": rev,
                        "stable_ver": stable_ver,
                    }
                )
                break  # only report the highest-priority channel

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
    triggered_by: str = "manual",
) -> tuple[bool, str]:
    """Dispatch a ``workflow_dispatch`` event to run YARF tests for the given snap.

    Creates a :class:`TestRun` record in the database before dispatching.

    Returns:
        A ``(success, error_message)`` tuple.  ``error_message`` is an empty
        string on success.
    """
    config = get_config()
    if not config.testing_repo:
        return False, "No testing_repo configured"
    if not config.github_token:
        return False, "No GitHub token configured"

    owner, _, repo = config.testing_repo.partition("/")
    if not repo:
        return False, f"Invalid testing_repo format: {config.testing_repo!r} (expected owner/repo)"

    # Persist a TestRun record first so we have a run_id to pass as an input.
    with get_session() as session:
        run = TestRun(
            snap_name=snap_name,
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
            return True, ""
        else:
            err = f"GitHub API returned {resp.status_code}: {resp.text[:300]}"
            with get_session() as session:
                run = session.query(TestRun).get(run_id)
                if run:
                    run.status = "error"
                    run.error_msg = err
            return False, err

    except httpx.RequestError as exc:
        err = str(exc)
        with get_session() as session:
            run = session.query(TestRun).get(run_id)
            if run:
                run.status = "error"
                run.error_msg = err
        return False, err


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
            pr = pr_map.get((run.snap_name, run.version or ""))
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
