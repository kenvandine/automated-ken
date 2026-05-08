"""Version bumper agent — opens a version-bump PR using the bot account."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from snap_dashboard.agents.base import BaseAgent
from snap_dashboard.auth import get_user_config
from snap_dashboard.db.models import UpstreamRelease, VersionBumpPR
from snap_dashboard.db.session import get_session
from snap_dashboard.github.bot_client import (
    BotGitHubClient,
    find_snapcraft_yaml,
    patch_snapcraft_yaml,
)

logger = logging.getLogger(__name__)


class VersionBumperAgent(BaseAgent):
    """Creates a branch and PR on the packaging repo to bump a snap version.

    Uses the bot GitHub account so the PR comes from a clearly identifiable
    automation user.
    """

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
        uc = get_user_config(self.user_id) if self.user_id else None
        bot_token = (uc.bot_github_token if uc else "") or ""
        if not bot_token:
            return f"skipped {self.snap_name}: no bot_github_token configured"

        # Safeguard: only one open PR per snap+part at a time
        if self._open_pr_exists():
            return (
                f"skipped {self.snap_name}/{self.part_name}: "
                f"open version bump PR already exists"
            )

        repo_slug = _extract_slug(self.packaging_repo)
        if not repo_slug:
            return f"skipped {self.snap_name}: cannot parse packaging_repo URL"
        owner, _, repo = repo_slug.partition("/")

        client = BotGitHubClient(bot_token)

        # Find snapcraft.yaml
        found = find_snapcraft_yaml(client, owner, repo)
        if not found:
            return f"skipped {self.snap_name}: snapcraft.yaml not found in {repo_slug}"
        yaml_path, yaml_content, yaml_sha = found

        # Patch the file
        patched = patch_snapcraft_yaml(yaml_content, self.part_name, self.new_version)
        if patched == yaml_content:
            return (
                f"skipped {self.snap_name}/{self.part_name}: "
                f"no source-tag found to patch in {yaml_path}"
            )

        # Create branch
        default_branch = client.get_default_branch(owner, repo)
        base_sha = client.get_branch_sha(owner, repo, default_branch)
        if not base_sha:
            return f"failed {self.snap_name}: cannot get SHA for branch {default_branch}"

        safe_version = self.new_version.replace("/", "-")
        branch_name = f"version-bump/{self.snap_name}/{safe_version}"

        if client.branch_exists(owner, repo, branch_name):
            return f"skipped {self.snap_name}: branch {branch_name} already exists"

        if not client.create_branch(owner, repo, branch_name, base_sha):
            return f"failed {self.snap_name}: could not create branch {branch_name}"

        # Commit the patched file
        commit_msg = (
            f"chore: update {self.part_name} to {self.new_version}\n\n"
            f"Automated version bump from {self.old_version} to {self.new_version}.\n"
            f"Upstream: {self.release_url}"
        )
        if not client.update_file(
            owner, repo, yaml_path, patched, yaml_sha, branch_name, commit_msg
        ):
            return f"failed {self.snap_name}: could not push updated {yaml_path}"

        # Generate PR title + body (with lemonade assist if available)
        title, body = self._build_pr_text(uc)

        pr = client.create_pr(
            owner=owner,
            repo=repo,
            title=title,
            body=body,
            head=branch_name,
            base=default_branch,
        )
        if not pr:
            return f"failed {self.snap_name}: PR creation failed"

        # Persist VersionBumpPR record
        with get_session() as session:
            release = session.query(UpstreamRelease).get(self.upstream_release_id)
            if release:
                release.acted_on = True
                release.acted_at = datetime.now(timezone.utc)

            bump = VersionBumpPR(
                snap_id=self.snap_id,
                upstream_release_id=self.upstream_release_id,
                user_id=self.user_id,
                bot_pr_url=pr.get("html_url"),
                bot_pr_number=pr.get("number"),
                packaging_repo=self.packaging_repo,
                branch_name=branch_name,
                old_version=self.old_version,
                new_version=self.new_version,
                status="open",
            )
            session.add(bump)

        logger.info(
            "version_bumper: opened PR for %s %s→%s: %s",
            self.snap_name, self.old_version, self.new_version, pr.get("html_url"),
        )
        return (
            f"opened PR for {self.snap_name} "
            f"{self.old_version}→{self.new_version}: {pr.get('html_url')}"
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _open_pr_exists(self) -> bool:
        with get_session() as session:
            return bool(
                session.query(VersionBumpPR)
                .filter(
                    VersionBumpPR.snap_id == self.snap_id,
                    VersionBumpPR.new_version == self.new_version,
                    VersionBumpPR.status.notin_(["merged", "closed"]),
                )
                .first()
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

        # Try to improve with lemonade
        lemonade = self._get_lemonade(uc)
        if lemonade:
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


def _extract_slug(repo: str) -> str | None:
    repo = repo.strip()
    if repo.startswith("https://github.com/"):
        repo = repo[len("https://github.com/"):]
    elif repo.startswith("http://github.com/"):
        repo = repo[len("http://github.com/"):]
    repo = repo.rstrip("/").rstrip(".git")
    if "/" not in repo:
        return None
    return repo
