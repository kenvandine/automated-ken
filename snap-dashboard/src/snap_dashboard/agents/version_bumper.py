"""Version bumper agent — delegates a version-bump PR to a coding agent.

Patching ``snapcraft.yaml`` used to be a hand-rolled, line-by-line regex
patch (see ``github.bot_client.patch_snapcraft_yaml``). That's fragile: it
only recognizes one exact ``source-tag:`` indentation/quoting shape, silently
does nothing for repos that pin the version a different way (a top-level
``version:`` only, a version baked into the source URL itself, an
``override-pull``/``override-build`` script, environment substitution,
etc.) — every one of those "no source-tag found to patch" skips is a snap
that silently never gets updated.

So bumping the version is now delegated to whichever "capable coding"
backend is configured (see ``agents/coding_backend.py`` — GitHub Copilot
cloud agent by default, or a local Lemonade coding model): it can actually
read the whole packaging repo, figure out how the version is pinned no
matter the shape, patch it correctly, and open the PR itself. The old
regex patch only remains as a last-resort fallback for the (increasingly
rare) case where no coding backend is configured/available at all.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from snap_dashboard.agents.base import BaseAgent
from snap_dashboard.agents.coding_backend import (
    extract_pr_number,
    extract_pr_url,
    get_coding_dispatcher,
    task_result_fields,
)
from snap_dashboard.auth import get_user_config
from snap_dashboard.db.models import UpstreamRelease, User, VersionBumpPR
from snap_dashboard.db.session import get_session
from snap_dashboard.github.bot_client import (
    BotGitHubClient,
    find_snapcraft_yaml,
    patch_snapcraft_yaml,
)
from snap_dashboard.github.utils import is_owned_by, parse_owner_repo

logger = logging.getLogger(__name__)


class VersionBumperAgent(BaseAgent):
    """Delegates a version-bump PR on the packaging repo to a coding agent."""

    agent_type = "version_bumper"

    def __init__(
        self,
        snap_id: int,
        snap_name: str,
        packaging_repo: str,
        upstream_release_id: int,
        part_name: str,
        old_version: str,
        new_version: str,
        release_url: str = "",
        release_notes: str = "",
        user_id: int | None = None,
    ) -> None:
        super().__init__(user_id=user_id, snap_name=snap_name)
        self.snap_id = snap_id
        self.packaging_repo = packaging_repo
        self.upstream_release_id = upstream_release_id
        self.part_name = part_name
        self.old_version = old_version
        self.new_version = new_version
        self.release_url = release_url
        self.release_notes = release_notes

    def _run(self) -> str:
        self._report(f"Checking if PR needed for {self.snap_name} {self.old_version}→{self.new_version}", self.snap_name)
        uc = get_user_config(self.user_id) if self.user_id else None
        bot_token = (uc.bot_github_token if uc else "") or ""
        if not bot_token:
            return f"skipped {self.snap_name}: no bot_github_token configured"

        # Safeguard: only one open PR per snap+part at a time
        if self._open_pr_exists():
            return (
                f"skipped {self.snap_name}/{self.part_name}: "
                f"open version bump PR already exists for this part"
            )

        owner_repo = parse_owner_repo(self.packaging_repo)
        if not owner_repo:
            return f"skipped {self.snap_name}: cannot parse packaging_repo URL"
        owner, repo = owner_repo

        login = ""
        if self.user_id:
            with get_session() as session:
                user = session.query(User).get(self.user_id)
                login = (user.github_login or "") if user else ""
        if not is_owned_by(self.packaging_repo, login):
            return (
                f"skipped {self.snap_name}: packaging_repo {owner}/{repo} is not "
                f"owned by {login or '(unknown user)'} — refusing to auto-bump a "
                "repo you don't own (check Settings for a misconfigured "
                "packaging_repo pointing at a third-party/upstream repo)"
            )

        bot_client = BotGitHubClient(
            bot_token,
            bot_login=(getattr(uc, "bot_github_login", None) if uc else None),
            read_token=(getattr(uc, "github_token", "") if uc else "") or bot_token,
        )
        found = find_snapcraft_yaml(bot_client, owner, repo)
        if not found:
            return f"skipped {self.snap_name}: snapcraft.yaml not found in {owner}/{repo}"
        yaml_path, yaml_content, yaml_sha = found
        default_branch = bot_client.get_default_branch(owner, repo)

        coding = get_coding_dispatcher(uc)
        if coding is None:
            self._report(
                f"No coding backend configured — falling back to regex patch for {self.snap_name}",
                self.snap_name,
            )
            return self._legacy_regex_bump(
                bot_client, owner, repo, yaml_path, yaml_content, yaml_sha, default_branch, uc,
            )

        self._report(f"Asking coding agent to bump {self.part_name} → {self.new_version}…", self.snap_name)
        prompt = self._build_bump_prompt(yaml_path)
        task = coding.start_task(owner, repo, prompt, base_ref=default_branch, create_pull_request=True)
        if not task:
            return f"failed {self.snap_name}: coding backend dispatch failed"

        fields = task_result_fields(task)
        return self._persist_from_task(task, fields, bot_client, owner, repo)

    # ------------------------------------------------------------------
    # Coding-agent path (primary)
    # ------------------------------------------------------------------

    def _build_bump_prompt(self, yaml_path: str) -> str:
        notes_block = f"\n\nUpstream release notes:\n{self.release_notes[:2000]}" if self.release_notes else ""
        return (
            f"This repo packages the '{self.snap_name}' snap. Its `{yaml_path}` "
            f"pins the '{self.part_name}' part at version {self.old_version!r}. A new "
            f"upstream release, {self.new_version!r}, is available "
            f"(release page: {self.release_url or 'n/a'}).{notes_block}\n\n"
            f"Update `{yaml_path}` so the '{self.part_name}' part builds "
            f"{self.new_version} instead of {self.old_version}. The version may be "
            "pinned via `source-tag`, a top-level `version:` field, a version "
            "embedded directly in the `source` URL, or some other mechanism this "
            "repo uses — inspect the actual file and make whatever change "
            "correctly bumps it, preserving the surrounding formatting and every "
            "other field. Don't touch any other part or file unless strictly "
            "necessary to make the bump work.\n\n"
            f"Open a single pull request titled roughly "
            f"'chore: update {self.part_name} to {self.new_version}' with a "
            "description explaining the version bump and linking the upstream "
            "release."
        )

    def _persist_from_task(self, task: dict, fields: dict, bot_client: BotGitHubClient, owner: str, repo: str) -> str:
        status = fields["status"]
        if status == "queued":
            # Async cloud-agent task — no PR yet, pr_monitor.py polls it.
            self._save_bump(status="dispatched", external_task_id=fields["external_task_id"])
            return (
                f"dispatched coding-agent task for {self.snap_name} "
                f"{self.old_version}→{self.new_version} (task {fields['external_task_id']})"
            )

        if status == "completed":
            pr_url = extract_pr_url(task)
            pr_number = extract_pr_number(task)
            if not pr_url or not pr_number:
                return f"failed {self.snap_name}: coding backend finished but produced no PR"
            branch_name = bot_client.get_pr_head_branch(owner, repo, pr_number) or ""
            self._save_bump(
                status="open", bot_pr_url=pr_url, bot_pr_number=pr_number, branch_name=branch_name,
            )
            logger.info(
                "version_bumper: opened PR for %s %s→%s: %s",
                self.snap_name, self.old_version, self.new_version, pr_url,
            )
            return (
                f"opened PR for {self.snap_name} "
                f"{self.old_version}→{self.new_version}: {pr_url}"
            )

        # dispatch_failed / failed / anything else
        error = task.get("error") or f"coding backend returned status={status!r}"
        return f"failed {self.snap_name}: {error}"

    def _save_bump(
        self,
        status: str,
        bot_pr_url: str | None = None,
        bot_pr_number: int | None = None,
        branch_name: str = "",
        external_task_id: str | None = None,
    ) -> None:
        with get_session() as session:
            release = session.query(UpstreamRelease).get(self.upstream_release_id)
            if release:
                release.acted_on = True
                release.acted_at = datetime.now(timezone.utc)

            bump = VersionBumpPR(
                snap_id=self.snap_id,
                upstream_release_id=self.upstream_release_id,
                user_id=self.user_id,
                bot_pr_url=bot_pr_url,
                bot_pr_number=bot_pr_number,
                packaging_repo=self.packaging_repo,
                branch_name=branch_name,
                old_version=self.old_version,
                new_version=self.new_version,
                status=status,
                external_task_id=external_task_id,
            )
            session.add(bump)

    # ------------------------------------------------------------------
    # Legacy regex path (fallback only — no coding backend configured)
    # ------------------------------------------------------------------

    def _legacy_regex_bump(
        self,
        client: BotGitHubClient,
        owner: str,
        repo: str,
        yaml_path: str,
        yaml_content: str,
        yaml_sha: str,
        default_branch: str,
        uc,
    ) -> str:
        patched = patch_snapcraft_yaml(yaml_content, self.part_name, self.new_version)
        if patched == yaml_content:
            return (
                f"skipped {self.snap_name}/{self.part_name}: "
                f"no source-tag found to patch in {yaml_path} (and no coding "
                "backend configured to attempt a smarter fix)"
            )

        base_sha = client.get_branch_sha(owner, repo, default_branch)
        if not base_sha:
            return f"failed {self.snap_name}: cannot get SHA for branch {default_branch}"

        # Write into the bot's own fork unless it's already the repo's
        # owner — the bot account is essentially never a collaborator on
        # the packaging repos it maintains, so writing directly into
        # owner/repo 404s. See BotGitHubClient.push_target().
        push_owner = client.push_target(owner, repo)

        safe_version = self.new_version.replace("/", "-")
        branch_name = f"version-bump/{self.snap_name}/{safe_version}"
        if client.branch_exists(push_owner, repo, branch_name):
            return f"skipped {self.snap_name}: branch {branch_name} already exists"
        if not client.create_branch(push_owner, repo, branch_name, base_sha):
            return f"failed {self.snap_name}: could not create branch {branch_name}"

        commit_msg = (
            f"chore: update {self.part_name} to {self.new_version}\n\n"
            f"Automated version bump from {self.old_version} to {self.new_version}.\n"
            f"Upstream: {self.release_url}"
        )
        if not client.update_file(push_owner, repo, yaml_path, patched, yaml_sha, branch_name, commit_msg):
            return f"failed {self.snap_name}: could not push updated {yaml_path}"

        title, body = self._build_pr_text(uc)
        pr = client.create_pr(
            owner=owner, repo=repo, title=title, body=body, head=branch_name, base=default_branch,
            head_owner=push_owner,
        )
        if not pr:
            return f"failed {self.snap_name}: PR creation failed"

        self._save_bump(
            status="open",
            bot_pr_url=pr.get("html_url"),
            bot_pr_number=pr.get("number"),
            branch_name=branch_name,
        )
        logger.info(
            "version_bumper: opened PR (regex fallback) for %s %s→%s: %s",
            self.snap_name, self.old_version, self.new_version, pr.get("html_url"),
        )
        return (
            f"opened PR for {self.snap_name} "
            f"{self.old_version}→{self.new_version}: {pr.get('html_url')}"
        )

    def _build_pr_text(self, uc) -> tuple[str, str]:
        default_title = f"chore: update {self.part_name} to {self.new_version}"
        default_body = (
            f"## Version bump: {self.part_name} {self.old_version} → {self.new_version}\n\n"
            f"Upstream release: {self.release_url}\n\n"
        )
        if self.release_notes:
            default_body += f"### Release notes\n\n{self.release_notes[:2000]}\n\n"
        default_body += (
            "_This PR was created automatically by snap-dashboard's version bumper agent._"
        )

        lemonade = self._get_lemonade(uc)
        if lemonade:
            self._report(f"⚡ Asking Lemonade AI to draft PR description for {self.snap_name}…", self.snap_name)
            result = lemonade.generate_pr_description(
                snap_name=self.snap_name,
                part_name=self.part_name,
                old_version=self.old_version,
                new_version=self.new_version,
                release_notes=self.release_notes,
            )
            if result:
                return result.get("title", default_title), result.get("body", default_body)

        return default_title, default_body

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _open_pr_exists(self) -> bool:
        """Return True if an unmerged, unclosed PR already exists for this snap+part.

        Checks via the UpstreamRelease join so we catch PRs for different
        versions of the same part (e.g. a v1.1 PR still open when v1.2
        arrives). A merged bump still working its way to stable doesn't
        block the next one.
        """
        with get_session() as session:
            existing = (
                session.query(VersionBumpPR)
                .join(VersionBumpPR.upstream_release)
                .filter(
                    VersionBumpPR.snap_id == self.snap_id,
                    VersionBumpPR.status.notin_(["merged", "closed"]),
                    VersionBumpPR.merged_at.is_(None),
                    UpstreamRelease.part_name == self.part_name,
                )
                .first()
            )
            return bool(existing)
