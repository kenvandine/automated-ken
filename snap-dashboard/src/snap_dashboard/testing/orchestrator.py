"""YARF test orchestration — find snaps needing tests, trigger workflows, sync status."""

from __future__ import annotations

import base64
import logging
import time
from datetime import datetime, timezone

import httpx

from snap_dashboard.db.models import ChannelMap, Snap, TestRun, TestRunScreenshot
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


def _default_branch(owner: str, repo: str, token: str) -> str:
    """Return the default branch name for *owner/repo*, falling back to ``main``."""
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(f"{_GH_API}/repos/{owner}/{repo}", headers=_gh_headers(token))
        resp.raise_for_status()
        return resp.json().get("default_branch", "main")
    except httpx.HTTPError:
        return "main"


def find_snaps_needing_tests(session, user_id: int | None = None) -> list[dict]:
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
    q = session.query(Snap).order_by(Snap.name)
    if user_id is not None:
        q = q.filter_by(user_id=user_id)
    snaps = q.all()
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

            stable_info = _get("stable")
            stable_ver = stable_info.get("version")
            stable_rev = stable_info.get("revision")
            candidate_info = _get("candidate")
            candidate_ver = candidate_info.get("version")
            candidate_rev = candidate_info.get("revision")

            # Candidate — promotion path to stable.
            # Include rebuilds: same version but higher revision (e.g. security rebuild).
            _candidate_differs = candidate_ver and (
                candidate_ver != stable_ver
                or (
                    candidate_rev is not None
                    and stable_rev is not None
                    and candidate_rev > stable_rev
                )
            )
            if _candidate_differs:
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


# Legacy shared repo that used to hold every snap's YARF suite under
# suites/<snap_name>/suite/. Tests are now colocated in each snap's own
# packaging repo (tests/suite/) instead — this is only used as a fallback
# for snaps whose packaging repo hasn't been bootstrapped with colocated
# tests yet.
LEGACY_CENTRAL_TESTING_REPO = "kenvandine/automated-ken-tests"


def _path_exists_in_repo(repo: str, path: str, token: str) -> bool:
    """Return True if *path* exists in *repo* (``owner/repo`` format)."""
    owner, _, name = repo.partition("/")
    if not name:
        return False
    url = f"{_GH_API}/repos/{owner}/{name}/contents/{path}"
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(url, headers=_gh_headers(token))
            return resp.status_code == 200
    except httpx.RequestError as exc:
        logger.warning("_path_exists_in_repo request failed for %s/%s: %s", repo, path, exc)
        return False


def resolve_test_repo(
    packaging_repo: str | None,
    snap_name: str,
    token: str,
    testing_repo_fallback: str = "",
) -> tuple[str, str]:
    """Return ``(repo, suite_path)`` for *snap_name*'s YARF test suite.

    Prefers the snap's own ``packaging_repo`` if it has been bootstrapped with
    colocated tests (``tests/suite/__init__.robot``). Falls back to the legacy
    shared testing repo (``suites/<snap_name>/suite/``) otherwise, so snaps not
    yet migrated keep working exactly as before.
    """
    if packaging_repo and _path_exists_in_repo(packaging_repo, "tests/suite/__init__.robot", token):
        return packaging_repo, "tests/suite"
    fallback = testing_repo_fallback or LEGACY_CENTRAL_TESTING_REPO
    return fallback, f"suites/{snap_name}/suite"


def suite_exists_in_repo(
    testing_repo: str,
    snap_name: str,
    token: str,
    packaging_repo: str | None = None,
) -> bool:
    """Return True if a YARF suite exists for *snap_name*.

    Checks the snap's own ``packaging_repo`` (colocated ``tests/suite/``) first,
    then falls back to ``suites/{snap_name}/suite/`` in *testing_repo*.
    """
    repo, path = resolve_test_repo(packaging_repo, snap_name, token, testing_repo)
    if not repo:
        return False
    return _path_exists_in_repo(repo, f"{path}/__init__.robot", token)


def trigger_workflow(
    snap_name: str,
    from_channel: str,
    version: str,
    revision: int | None,
    architecture: str = "amd64",
    triggered_by: str = "manual",
    testing_repo: str = "",
    github_token: str = "",
    user_id: int | None = None,
    packaging_repo: str | None = None,
) -> tuple[bool, str, int | None]:
    """Dispatch a ``workflow_dispatch`` event to run YARF tests for the given snap.

    Prefers *packaging_repo* if it has been bootstrapped with a colocated
    YARF suite (``tests/suite/``); falls back to *testing_repo* (or the
    legacy shared testing repo) otherwise. Creates a :class:`TestRun` record
    in the database before dispatching, recording which repo was used.

    Returns:
        A ``(success, error_message, db_run_id)`` tuple.  ``error_message`` is
        an empty string on success; ``db_run_id`` is the new TestRun PK or None
        on failure.
    """
    if not github_token:
        from snap_dashboard.config import get_config
        cfg = get_config()
        github_token = cfg.github_token

    if not github_token:
        return False, "No GitHub token configured", None

    resolved_repo, _suite_path = resolve_test_repo(
        packaging_repo, snap_name, github_token, testing_repo
    )
    if not resolved_repo:
        return False, "No testing repo configured or discoverable", None

    owner, _, repo = resolved_repo.partition("/")
    if not repo:
        return False, f"Invalid repo format: {resolved_repo!r} (expected owner/repo)", None

    ref = _default_branch(owner, repo, github_token)

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
            user_id=user_id,
            repo=resolved_repo,
        )
        session.add(run)
        session.flush()
        run_id = run.id

    url = f"{_GH_API}/repos/{owner}/{repo}/actions/workflows/snap-test.yml/dispatches"
    payload = {
        "ref": ref,
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
            resp = client.post(url, json=payload, headers=_gh_headers(github_token))

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


def trigger_remote_run(
    snap_name: str,
    from_channel: str,
    version: str,
    revision: int | None,
    architecture: str = "amd64",
    triggered_by: str = "manual",
    runner_id: int | None = None,
    priority: int = 0,
    user_id: int | None = None,
) -> tuple[bool, str, int | None]:
    """Queue a ``TestRun`` for a registered remote runner instead of GitHub Actions.

    No outbound network call is made here — the run just sits in the queue
    with ``status="pending"`` until an eligible runner's
    ``GET /api/runners/{id}/next-job`` poll claims it (see
    ``web/routes/runner_api.py``). If ``runner_id`` is None, any idle
    runner belonging to the same user may claim it.

    Returns a ``(success, error_message, db_run_id)`` tuple, mirroring
    :func:`trigger_workflow`.
    """
    with get_session() as session:
        if runner_id is not None:
            from snap_dashboard.db.models import Runner

            runner = session.query(Runner).get(runner_id)
            if runner is None or runner.revoked_at is not None:
                return False, f"Runner {runner_id} not found or revoked", None

        run = TestRun(
            snap_name=snap_name,
            architecture=architecture,
            from_channel=from_channel,
            version=version,
            revision=revision,
            status="pending",
            triggered_by=triggered_by,
            dispatch_target="remote_runner",
            runner_id=runner_id,
            priority=priority,
            user_id=user_id,
        )
        session.add(run)
        session.flush()
        return True, "", run.id


def poll_for_gh_run_id(
    db_run_id: int,
    triggered_at: datetime,
    testing_repo: str = "",
    github_token: str = "",
) -> None:
    """Background task: find the GH Actions run for our dispatch then monitor it to completion.

    Phase 1 — find the run ID by polling the Actions API (up to ~3 min).
    Phase 2 — poll the run's status every 30s until it reaches a terminal
               state (passed/failed) or 90 minutes elapse.

    Status updates are written directly to the DB so the JS polling picks
    them up without a manual sync.
    """
    if not testing_repo or not github_token:
        # Fall back to global config
        from snap_dashboard.config import get_config
        cfg = get_config()
        testing_repo = testing_repo or cfg.testing_repo
        github_token = github_token or cfg.github_token

    if not testing_repo or not github_token:
        return
    owner, _, repo = testing_repo.partition("/")
    if not repo:
        return

    headers = _gh_headers(github_token)

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
        new_status = _check_gh_run_status(gh_run_id, owner, repo, github_token)
        if new_status is None:
            continue
        with get_session() as session:
            db_run = session.query(TestRun).get(db_run_id)
            if db_run and db_run.status != new_status:
                db_run.status = new_status
                if new_status in ("passed", "failed"):
                    db_run.finished_at = datetime.now(timezone.utc)
        if new_status in ("passed", "failed"):
            ingest_run_screenshots(db_run_id, gh_run_id, owner, repo, github_token)
            if new_status == "passed":
                maybe_submit_auto_promoter(db_run_id)
            logger.info("poll_for_gh_run_id: run %s finished as %s", gh_run_id, new_status)
            return


def ingest_run_screenshots(
    db_run_id: int,
    gh_run_id: str | None,
    owner: str,
    repo: str,
    token: str,
) -> None:
    """Best-effort: download the run's ``yarf-results-*`` artifact and store its PNGs.

    Populates :class:`TestRunScreenshot` so the screenshot reviewer / auto-promoter
    agents can compare real screenshots instead of always falling back to
    "no comparable screenshots available". Safe to call repeatedly — a no-op if
    screenshots were already ingested for this run, or if anything fails.
    """
    if not gh_run_id or not token:
        return
    with get_session() as session:
        if session.query(TestRunScreenshot).filter_by(test_run_id=db_run_id).first():
            return  # already ingested

    from snap_dashboard.github.artifacts import fetch_run_screenshots

    try:
        pngs = fetch_run_screenshots(owner, repo, gh_run_id, token)
    except Exception as exc:
        logger.warning("ingest_run_screenshots: failed for TestRun %s (gh_run=%s): %s", db_run_id, gh_run_id, exc)
        return

    if not pngs:
        return

    with get_session() as session:
        if session.query(TestRunScreenshot).filter_by(test_run_id=db_run_id).first():
            return  # ingested concurrently
        for image_name, data in pngs:
            session.add(
                TestRunScreenshot(
                    test_run_id=db_run_id,
                    image_name=image_name,
                    image_b64=base64.b64encode(data).decode(),
                )
            )
    logger.info("ingest_run_screenshots: stored %d screenshot(s) for TestRun %s", len(pngs), db_run_id)


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


def sync_test_runs(
    testing_repo: str = "",
    github_token: str = "",
    user_id: int | None = None,
) -> None:
    """Poll GitHub for open test PRs and reconcile with local :class:`TestRun` records.

    - Updates existing *pending/triggered/running* runs with PR metadata.
    - Creates new ``TestRun`` records for PRs that arrived without a prior dispatch
      (e.g. runs triggered externally or before the dashboard was set up).
    """
    if not testing_repo or not github_token:
        from snap_dashboard.config import get_config
        cfg = get_config()
        testing_repo = testing_repo or cfg.testing_repo
        github_token = github_token or cfg.github_token

    if not testing_repo or not github_token:
        return

    owner, _, repo = testing_repo.partition("/")
    if not repo:
        return

    from snap_dashboard.github.pr_viewer import get_test_prs, parse_pr_metadata

    try:
        prs = get_test_prs(testing_repo, github_token)
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

    # Track TestRuns whose status just flipped to "passed" so we can queue
    # auto-promotion for them, and (test_run_id, gh_run_id) pairs whose
    # status just went terminal this pass — populated below and acted on
    # once every DB session in this function has been closed.
    auto_promote_run_ids: set[int] = set()
    screenshot_ingest_targets: list[tuple[int, str]] = []

    # Read the in-flight runs into plain dicts and close the session before
    # making any GitHub API calls below. _check_gh_run_status() can block
    # for up to its httpx timeout per in-flight run; doing that inside an
    # open write transaction would hold SQLite's single writer lock for the
    # whole loop and starve every other writer (background agents, the web
    # UI) with "database is locked" until it finally finished or failed.
    with get_session() as session:
        q = session.query(TestRun).filter(
            TestRun.status.in_(["pending", "triggered", "running"])
        )
        if user_id is not None:
            q = q.filter_by(user_id=user_id)
        in_flight = [
            {
                "id": r.id,
                "snap_name": r.snap_name,
                "version": r.version,
                "status": r.status,
                "gh_run_id": r.gh_run_id,
                "promoted": r.promoted,
            }
            for r in q.all()
        ]

    # Now do all the (potentially slow) GitHub API lookups with no DB
    # session/transaction open.
    gh_status_by_run_id: dict[int, str] = {}
    for run in in_flight:
        pr = pr_map.get((run["snap_name"], run["version"] or ""))
        if not pr and run["gh_run_id"]:
            gha_status = _check_gh_run_status(run["gh_run_id"], owner, repo, github_token)
            if gha_status and gha_status != run["status"]:
                gh_status_by_run_id[run["id"]] = gha_status

    with get_session() as session:
        for run_info in in_flight:
            pr = pr_map.get((run_info["snap_name"], run_info["version"] or ""))
            if not pr and run_info["gh_run_id"]:
                gha_status = gh_status_by_run_id.get(run_info["id"])
                if gha_status:
                    run = session.query(TestRun).get(run_info["id"])
                    if run:
                        run.status = gha_status
                        if gha_status in ("passed", "failed"):
                            run.finished_at = datetime.now(timezone.utc)
                            screenshot_ingest_targets.append((run.id, run.gh_run_id))
                continue
            if not pr:
                continue
            run = session.query(TestRun).get(run_info["id"])
            if not run:
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
                if run.gh_run_id:
                    screenshot_ingest_targets.append((run.id, run.gh_run_id))
                if gh_status == "passed" and not run.promoted:
                    auto_promote_run_ids.add(run.id)

        # Create stubs for externally-triggered PRs we have no record for
        q2 = session.query(TestRun)
        if user_id is not None:
            q2 = q2.filter_by(user_id=user_id)
        known_keys = {
            (r.snap_name, r.version or "")
            for r in q2.all()
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
                user_id=user_id,
            )
            if new_run.status in ("passed", "failed"):
                new_run.finished_at = datetime.now(timezone.utc)
            session.add(new_run)
            session.flush()
            if new_run.status in ("passed", "failed") and new_run.gh_run_id:
                screenshot_ingest_targets.append((new_run.id, new_run.gh_run_id))
            if new_run.status == "passed" and not new_run.promoted:
                auto_promote_run_ids.add(new_run.id)

    for run_id, gh_run_id in screenshot_ingest_targets:
        ingest_run_screenshots(run_id, gh_run_id, owner, repo, github_token)

    for run_id in sorted(auto_promote_run_ids):
        maybe_submit_auto_promoter(run_id)


def maybe_submit_auto_promoter(test_run_id: int) -> None:
    """Queue candidate auto-promotion for a passed test run when configured."""
    with get_session() as session:
        run = session.query(TestRun).get(test_run_id)
        if not run or run.promoted or run.status != "passed" or run.from_channel != "candidate":
            return
        user_id = run.user_id
        if user_id is None:
            return
        run.status = "reviewing"

    from snap_dashboard.auth import get_user_config
    uc = get_user_config(user_id)
    if not getattr(uc, "auto_promote", False):
        return

    from snap_dashboard.agents.runner import get_runner
    from snap_dashboard.agents.test_run_auto_promoter import TestRunAutoPromoterAgent

    get_runner().submit(TestRunAutoPromoterAgent(test_run_id=test_run_id, user_id=user_id))
