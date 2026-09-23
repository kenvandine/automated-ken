"""Fleet-normalization agent — one-time repo-consistency campaign, delegated to Copilot.

Early on, snaps in the fleet accumulated their own bespoke per-repo automation
(most commonly a daily ``sync-release`` workflow that polls upstream for new
versions). Now that automated-ken drives version-checking, testing, and
promotion centrally, those repos need to be brought in line. Rather than
having automated-ken hand-edit files itself (fragile pattern matching for
"find the sync-release workflow", reinventing doc-writing, etc.), this agent
delegates the whole first pass to **GitHub Copilot cloud agent** per repo —
it can actually read the existing workflow, understand what it does, write a
sensible AGENTS.md, and open a PR, which is squarely "capable coding" work.

This is a one-time catch-up campaign, not a periodic agent — it's triggered
manually from the dashboard (opt-in via ``UserConfig.fleet_normalization_enabled``)
so each repo's owner can review the PRs Copilot opens before merging them.
"""

from __future__ import annotations

import logging

from snap_dashboard.agents.base import BaseAgent
from snap_dashboard.auth import get_user_config
from snap_dashboard.db.models import CopilotTask, Snap
from snap_dashboard.db.session import get_session
from snap_dashboard.github.bot_client import BotGitHubClient
from snap_dashboard.agents.coding_backend import CodingDispatcher, get_coding_dispatcher
from snap_dashboard.github.utils import parse_owner_repo
from snap_dashboard.testing.suite_zip import list_suite_files

logger = logging.getLogger(__name__)

# Cap how much suite content gets inlined into a single prompt — Copilot cloud
# agent tasks have a prompt size limit, and huge binary fixtures wouldn't
# survive being pasted as text anyway.
_MAX_INLINE_SUITE_BYTES = 40_000


class RepoNormalizerAgent(BaseAgent):
    """One-time fleet-wide consistency campaign, triggered manually from the dashboard."""

    agent_type = "repo_normalizer"

    def __init__(self, user_id: int | None = None) -> None:
        super().__init__(user_id=user_id)

    def _run(self) -> str:
        if not self.user_id:
            return "no user_id — skipped"
        uc = get_user_config(self.user_id)
        if not uc or not getattr(uc, "fleet_normalization_enabled", False):
            return "disabled"
        token = (getattr(uc, "bot_github_token", "") or getattr(uc, "github_token", "") or "")
        if not token:
            return "no GitHub token configured"
        testing_repo = getattr(uc, "testing_repo", "") or ""

        with get_session() as session:
            snaps = [
                (s.id, s.name, s.packaging_repo)
                for s in session.query(Snap).filter_by(user_id=self.user_id).all()
                if s.packaging_repo
            ]

        if not snaps:
            return "no packaging repos found"

        bot_client = BotGitHubClient(token)
        copilot = get_coding_dispatcher(uc)
        if not copilot:
            return "no coding backend configured/available"
        dispatched, skipped = 0, 0
        for snap_id, snap_name, packaging_repo in snaps:
            self._report(f"Normalizing {packaging_repo}", snap_name)
            try:
                did = self._normalize_repo(bot_client, copilot, snap_id, snap_name, packaging_repo, testing_repo, token)
            except Exception as exc:
                logger.exception("repo_normalizer: failed for %s: %s", packaging_repo, exc)
                did = False
            if did:
                dispatched += 1
            else:
                skipped += 1

        return f"dispatched {dispatched} normalization task(s), {skipped} skipped/already-done"

    def _normalize_repo(
        self,
        bot_client: BotGitHubClient,
        copilot: CodingDispatcher,
        snap_id: int,
        snap_name: str,
        packaging_repo: str,
        testing_repo: str,
        token: str,
    ) -> bool:
        owner_repo = parse_owner_repo(packaging_repo)
        if not owner_repo:
            return False
        owner, repo = owner_repo
        owner_repo_str = f"{owner}/{repo}"

        # Idempotency: skip repos that already have an AGENTS.md, or that
        # already have a fleet_normalize task in flight/done.
        if bot_client.file_exists(owner, repo, "AGENTS.md"):
            return False
        with get_session() as session:
            existing = (
                session.query(CopilotTask)
                .filter(CopilotTask.kind == "fleet_normalize", CopilotTask.owner_repo == owner_repo_str)
                .first()
            )
            if existing:
                return False

        suite_block, moved_suite = self._build_suite_block(testing_repo, snap_name, token)

        prompt = (
            f"This repo packages the '{snap_name}' snap and is now maintained by "
            "automated-ken (https://github.com/kenvandine/automated-ken), a "
            "self-hosted agentic snap-maintenance dashboard. Please bring this repo "
            "in line with how automated-ken now maintains it:\n\n"
            "1. Find any workflow under .github/workflows/ that polls upstream for "
            "new releases on a schedule (commonly named something like "
            "'sync-release'). Remove it — automated-ken now polls for new upstream "
            "releases centrally across the whole snap fleet, so this per-repo "
            "workflow is redundant and would fight with it.\n"
            "2. Add an AGENTS.md at the repo root explaining that automated-ken now "
            "owns version detection, opening version-bump PRs, CI monitoring "
            "(including asking Copilot cloud agent to fix a failing build workflow "
            "on its own follow-up PR), YARF testing, and channel promotion "
            "(edge -> candidate -> stable) for this snap. Mention that maintainers/"
            "agents shouldn't hand-edit the pinned version, and that the removed "
            "workflow's job is now automated-ken's responsibility.\n"
            + (
                "3. Add the YARF test suite below under tests/ in this repo (create "
                "the directory structure exactly as given, preserving relative paths):\n\n"
                f"{suite_block}\n"
                if moved_suite
                else "3. There is no test suite to migrate for this snap right now — skip this step.\n"
            )
            + "\nOpen a single pull request with all of the above changes."
        )

        task = copilot.start_task(owner, repo, prompt, base_ref="main", create_pull_request=True)
        with get_session() as session:
            session.add(
                CopilotTask(
                    user_id=self.user_id,
                    snap_id=snap_id,
                    kind="fleet_normalize",
                    owner_repo=owner_repo_str,
                    external_task_id=str(task.get("id")) if task else None,
                    prompt=prompt,
                    status="queued" if task else "dispatch_failed",
                )
            )
        if task and moved_suite and testing_repo:
            self._dispatch_suite_cleanup(copilot, snap_id, snap_name, testing_repo, packaging_repo)
        return bool(task)

    @staticmethod
    def _build_suite_block(testing_repo: str, snap_name: str, token: str) -> tuple[str, bool]:
        """Return (markdown code blocks for each suite file, whether any suite was found)."""
        if not testing_repo:
            return "", False
        suite_files = list_suite_files(testing_repo, snap_name, token)
        if not suite_files:
            return "", False

        blocks = []
        total = 0
        for rel_path, content in suite_files.items():
            try:
                text = content.decode()
            except UnicodeDecodeError:
                blocks.append(f"(binary file `tests/{rel_path}` skipped — please fetch it manually if needed)")
                continue
            total += len(text)
            if total > _MAX_INLINE_SUITE_BYTES:
                blocks.append(
                    "(remaining suite files omitted for length — fetch the rest from "
                    f"`suites/{snap_name}/suite/` in {testing_repo})"
                )
                break
            blocks.append(f"`tests/{rel_path}`:\n```\n{text}\n```")
        return "\n\n".join(blocks), True

    def _dispatch_suite_cleanup(
        self, copilot: CodingDispatcher, snap_id: int, snap_name: str, testing_repo: str, packaging_repo: str,
    ) -> None:
        owner_repo = parse_owner_repo(testing_repo)
        if not owner_repo:
            return
        owner, repo = owner_repo
        owner_repo_str = f"{owner}/{repo}"
        with get_session() as session:
            existing = (
                session.query(CopilotTask)
                .filter(
                    CopilotTask.kind == "fleet_normalize",
                    CopilotTask.owner_repo == owner_repo_str,
                    CopilotTask.issue_number == snap_id,
                )
                .first()
            )
            if existing:
                return

        prompt = (
            f"The '{snap_name}' YARF test suite under suites/{snap_name}/ has been "
            f"moved to its own packaging repo ({packaging_repo}), where it now lives "
            "under tests/. Please remove suites/"
            f"{snap_name}/ from this repo and open a pull request for the removal."
        )
        task = copilot.start_task(owner, repo, prompt, base_ref="main", create_pull_request=True)
        with get_session() as session:
            session.add(
                CopilotTask(
                    user_id=self.user_id,
                    snap_id=snap_id,
                    kind="fleet_normalize",
                    owner_repo=owner_repo_str,
                    external_task_id=str(task.get("id")) if task else None,
                    prompt=prompt,
                    status="queued" if task else "dispatch_failed",
                    issue_number=snap_id,  # repurposed here as a "which snap" dedupe key
                )
            )
