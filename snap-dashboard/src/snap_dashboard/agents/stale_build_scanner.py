"""Stale build scanner agent — finds snaps not published recently and triggers rebuilds."""

from __future__ import annotations

import json
import logging
import re
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


def _record_trigger(
    snap: dict,
    channel: str,
    status: str,
    days_since_publish: int | None = None,
    error: str | None = None,
    workflow_file: str | None = None,
) -> None:
    with get_session() as session:
        trigger = StaleBuildTrigger(
            snap_id=snap["id"],
            user_id=snap["user_id"],
            packaging_repo=snap["packaging_repo"],
            channel=channel,
            days_since_publish=days_since_publish,
            status=status,
            error_msg=error,
            workflow_file=workflow_file,
        )
        session.add(trigger)


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
            _record_trigger(
                snap, channel=_BUILD_CHANNEL, days_since_publish=days_stale,
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
            _record_trigger(snap, channel=_BUILD_CHANNEL, days_since_publish=days_stale, status="triggered")
            logger.info("stale_build_scanner: triggered rebuild for %s", snap["name"])
            return "triggered"
        else:
            _record_trigger(
                snap, channel=_BUILD_CHANNEL, days_since_publish=days_stale,
                status="failed", error=err,
            )
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


def _list_workflow_paths(client, owner: str, repo: str) -> list[str]:
    """Return every ``.github/workflows/*.yml``/``*.yaml`` path in the repo."""
    return [
        p
        for p in client.list_tree(owner, repo)
        if p.startswith(".github/workflows/") and p.endswith((".yml", ".yaml"))
    ]


def _dispatchable_workflows(client, owner: str, repo: str) -> list[tuple[str, str]]:
    """Return ``[(path, content)]`` for workflows that declare
    ``workflow_dispatch`` — only these can be triggered via the Actions
    dispatch API, so anything else (push/schedule-only workflows) isn't a
    candidate at all.
    """
    candidates = []
    for path in _list_workflow_paths(client, owner, repo):
        got = client.get_file(owner, repo, path)
        if not got:
            continue
        content, _sha = got
        if re.search(r"(?m)^\s*workflow_dispatch\s*:", content):
            candidates.append((path, content))
    return candidates


def _heuristic_pick_workflow(candidates: list[tuple[str, str]]) -> str:
    """Best-effort guess when no local model is available: prefer filenames
    that look build/publish flavored over e.g. a lint/test-only workflow."""
    keywords = ("snap", "build", "publish", "release")
    for path, _content in candidates:
        name = path.rsplit("/", 1)[-1].lower()
        if any(k in name for k in keywords):
            return path.rsplit("/", 1)[-1]
    return candidates[0][0].rsplit("/", 1)[-1]


def _infer_build_workflow(
    candidates: list[tuple[str, str]], snap_name: str, user_config
) -> tuple[str | None, str | None]:
    """Pick which dispatchable workflow builds+publishes the snap package.

    Repos don't necessarily name their build/publish workflow
    ``automated-snap-build.yml`` — plenty of packaging repos have their own
    hand-written workflow under an arbitrary filename. When there's exactly
    one dispatchable workflow it's used directly (no need to ask); when
    there's more than one, a local Lemonade model reads each workflow's YAML
    and picks the one that actually builds the snap (snapcraft) and
    publishes/uploads it to the Snap Store, falling back to a filename
    heuristic if no model is available or it can't parse a valid answer.

    Returns ``(workflow_filename, note)`` — ``note`` is a short explanation
    when a heuristic/fallback was used (for logging), or ``None`` when the
    model made the call (or there was nothing to choose between).
    """
    if not candidates:
        return None, None
    if len(candidates) == 1:
        return candidates[0][0].rsplit("/", 1)[-1], None

    from snap_dashboard.lemonade.client import get_lemonade_client

    valid_names = {path.rsplit("/", 1)[-1] for path, _content in candidates}
    client = get_lemonade_client(user_config, task="text") if user_config else None
    if client is None or not client.is_available():
        return _heuristic_pick_workflow(candidates), "no local model available — used heuristic"

    listing = "\n\n".join(
        f"### {path}\n```yaml\n{content[:4000]}\n```" for path, content in candidates
    )
    prompt = (
        f"This GitHub repo packages the snap '{snap_name}'. It has multiple "
        "GitHub Actions workflows that can be manually dispatched "
        "(workflow_dispatch). Identify which ONE of them builds the snap "
        "package (snapcraft) and publishes/uploads it to the Snap Store "
        "(e.g. via `snapcraft upload`, `snapcraft push`, or the "
        "snapcore/action-publish action).\n\n"
        f"{listing}\n\n"
        'Respond with ONLY a JSON object: {"workflow_file": "<filename>"}'
    )
    reply = client.chat(prompt, temperature=0.1)
    if not reply:
        return _heuristic_pick_workflow(candidates), "model call failed — used heuristic"
    try:
        start = reply.find("{")
        end = reply.rfind("}") + 1
        chosen = json.loads(reply[start:end]).get("workflow_file", "")
    except Exception:
        chosen = ""
    if chosen in valid_names:
        return chosen, None
    return (
        _heuristic_pick_workflow(candidates),
        "model returned an unrecognized filename — used heuristic",
    )


def _dispatch_rebuild_for_snap(
    client, snap: dict, user_config=None
) -> tuple[str, str | None]:
    """Dispatch an immediate rebuild for one snap's packaging repo.

    Shared by :class:`RebuildAllSnapsAgent` (loops over every snap) and
    :class:`RebuildOneSnapAgent` (a single snap, from its detail page or
    the snaps list). Prefers our own generated ``automated-snap-build.yml``
    when present (no inference needed), but falls back to inspecting
    whatever dispatchable workflow(s) the repo already has and inferring
    the right one to trigger — see ``_infer_build_workflow`` — rather than
    requiring that exact filename. Returns ``(status, error)`` where
    ``status`` is one of ``"triggered"``, ``"no_repo"`` (no/non-GitHub
    packaging repo), ``"no_workflow"`` (repo has no dispatchable
    build/publish workflow at all — this deliberately does not create one,
    unlike the staleness scanner), or ``"error"`` (dispatch itself failed).
    """
    owner_repo = _parse_github_owner_repo(snap["packaging_repo"])
    if not owner_repo:
        _record_trigger(snap, channel=_BUILD_CHANNEL, status="skipped", error="packaging repo isn't a GitHub URL")
        return "no_repo", None
    owner, repo = owner_repo

    if client.file_exists(owner, repo, WORKFLOW_PATH):
        workflow_file = _BUILD_WORKFLOW
        dispatch_inputs = {"dashboard_trigger_id": str(snap["id"])}
    else:
        candidates = _dispatchable_workflows(client, owner, repo)
        if not candidates:
            logger.debug(
                "rebuild: %s has no dispatchable build/publish workflow, skipping",
                snap["name"],
            )
            _record_trigger(
                snap, channel=_BUILD_CHANNEL, status="skipped",
                error="no dispatchable (workflow_dispatch) build workflow found in packaging repo",
            )
            return "no_workflow", None
        workflow_file, note = _infer_build_workflow(candidates, snap["name"], user_config)
        if note:
            logger.info("rebuild: %s — %s", snap["name"], note)
        if not workflow_file:
            _record_trigger(
                snap, channel=_BUILD_CHANNEL, status="skipped",
                error="could not determine which workflow builds/publishes this snap",
            )
            return "no_workflow", None
        # Only our own generated workflow declares dashboard_trigger_id —
        # an arbitrary repo-owned workflow may reject unrecognized inputs.
        dispatch_inputs = None

    default_branch = client.get_default_branch(owner, repo)
    success, err = client.dispatch_workflow(
        owner, repo, workflow_file,
        ref=default_branch,
        inputs=dispatch_inputs,
    )
    if success:
        _record_trigger(snap, channel=_BUILD_CHANNEL, status="triggered", workflow_file=workflow_file)
        logger.info("rebuild: triggered %s via %s", snap["name"], workflow_file)
        return "triggered", None
    else:
        _record_trigger(
            snap, channel=_BUILD_CHANNEL, status="failed", error=err, workflow_file=workflow_file
        )
        logger.warning("rebuild: dispatch failed for %s: %s", snap["name"], err)
        return "error", err


class RebuildAllSnapsAgent(BaseAgent):
    """Manually triggers an immediate rebuild for every snap with a GitHub
    packaging repo that already has a dispatchable build/publish workflow
    (our own generated one, or an existing hand-written one — see
    ``_infer_build_workflow``).

    Unlike :class:`StaleSnapScannerAgent`, this ignores publish staleness
    entirely (every eligible snap gets a rebuild) and does **not** create
    a workflow in repos that don't have any dispatchable one at all —
    "rebuild everything that already has a build workflow" is a
    deliberate, immediate, user-initiated action (Settings → "Rebuild All
    Snaps Now"), not the same policy as the automatic staleness-based
    scanner.
    """

    agent_type = "rebuild_all_snaps"

    def __init__(self, user_id: int | None = None) -> None:
        super().__init__(user_id=user_id)

    def _run(self) -> str:
        uc = get_user_config(self.user_id) if self.user_id else None
        token = (getattr(uc, "github_token", "") or "") if uc else ""
        if not token:
            return "no GitHub token configured — skipping"

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
        self._report(f"Rebuilding {total} snaps with a GitHub packaging repo…")

        from snap_dashboard.github.bot_client import BotGitHubClient
        client = BotGitHubClient(token=token)

        triggered = 0
        no_workflow = 0
        no_repo = 0
        errors = 0

        for i, snap in enumerate(snaps, 1):
            self._report(f"Checking {i}/{total}: {snap['name']}", snap["name"])
            status, _err = _dispatch_rebuild_for_snap(client, snap, user_config=uc)
            if status == "triggered":
                triggered += 1
            elif status == "no_workflow":
                no_workflow += 1
            elif status == "no_repo":
                no_repo += 1
            else:
                errors += 1

        return (
            f"{triggered} rebuild(s) triggered, {no_workflow} skipped (no build workflow), "
            f"{no_repo} skipped (no GitHub packaging repo), {errors} error(s)"
        )


class RebuildOneSnapAgent(BaseAgent):
    """Manually triggers an immediate rebuild for a single snap, the same
    way :class:`RebuildAllSnapsAgent` does for the whole fleet — used by
    the per-snap "Rebuild Now" button on the snap detail page and the
    dashboard's snap list. Requires the snap to already have a GitHub
    packaging repo with some dispatchable build/publish workflow (ours or
    a pre-existing hand-written one — see ``_infer_build_workflow``); it
    does not create a workflow if none exists at all (use "Check
    Updates"/the stale-build scanner for that).
    """

    agent_type = "rebuild_one_snap"

    def __init__(self, user_id: int | None = None, snap_id: int | None = None) -> None:
        super().__init__(user_id=user_id)
        self.snap_id = snap_id

    def _run(self) -> str:
        uc = get_user_config(self.user_id) if self.user_id else None
        token = (getattr(uc, "github_token", "") or "") if uc else ""
        if not token:
            return "no GitHub token configured — skipping"

        with get_session() as session:
            q = session.query(Snap).filter_by(id=self.snap_id)
            if self.user_id:
                q = q.filter_by(user_id=self.user_id)
            s = q.first()
            if not s:
                return "snap not found — skipping"
            snap = {
                "id": s.id,
                "name": s.name,
                "packaging_repo": s.packaging_repo or "",
                "user_id": s.user_id,
            }

        if not snap["packaging_repo"]:
            return f"{snap['name']}: no GitHub packaging repo configured — skipping"

        self._report(f"Rebuilding {snap['name']}…", snap["name"])

        from snap_dashboard.github.bot_client import BotGitHubClient
        client = BotGitHubClient(token=token)

        status, err = _dispatch_rebuild_for_snap(client, snap, user_config=uc)
        if status == "triggered":
            return f"{snap['name']}: rebuild triggered"
        elif status == "no_workflow":
            return f"{snap['name']}: skipped — no dispatchable build/publish workflow found in packaging repo"
        elif status == "no_repo":
            return f"{snap['name']}: skipped — packaging repo isn't a GitHub URL"
        else:
            return f"{snap['name']}: dispatch failed — {err}"

