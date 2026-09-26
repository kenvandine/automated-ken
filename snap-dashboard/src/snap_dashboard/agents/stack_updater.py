"""Stack update agent — manual "check for framework/dependency updates" trigger.

Some snaps package a project where the same GitHub repo is *both* the
upstream source and the packaging wrapper (a personal Electron/Rust/etc.
app the user maintains themselves, rather than a separate third-party
project wrapped in its own snapcraft.yaml repo). For these, staying current
matters in two dimensions at once: the language/package-manager
dependencies (npm, cargo, pip, go.mod, ...) *and* the runtime framework
itself (e.g. bumping the pinned Electron version), since an outdated
Electron bundles an outdated, potentially-vulnerable Chromium/Node.

This is deliberately a manual, one-off trigger from the snap detail page
(the "Check for Stack Updates" button) rather than a scheduled agent —
unlike ``UpstreamMaintainerAgent``'s npm-only ``dep_update`` (which assumes
a separate upstream repo and only ever looks at package.json), this asks
Copilot cloud agent to first figure out *what* stack the repo uses at all
before deciding what "check for updates" even means for it.
"""

from __future__ import annotations

import logging

from snap_dashboard.agents.base import BaseAgent
from snap_dashboard.agents.coding_backend import get_coding_dispatcher, task_result_fields
from snap_dashboard.auth import get_user_config
from snap_dashboard.db.models import CopilotTask, Snap
from snap_dashboard.db.session import get_session
from snap_dashboard.github.bot_client import BotGitHubClient
from snap_dashboard.github.utils import parse_owner_repo

logger = logging.getLogger(__name__)


class StackUpdateAgent(BaseAgent):
    """One-off "review this repo's stack for outdated deps/framework" dispatch."""

    agent_type = "stack_updater"

    def __init__(self, user_id: int | None = None, snap_id: int | None = None) -> None:
        super().__init__(user_id=user_id)
        self.snap_id = snap_id

    def _run(self) -> str:
        if not self.user_id or not self.snap_id:
            return "no user_id/snap_id — skipped"
        uc = get_user_config(self.user_id)
        if not uc:
            return "no user config"
        token = getattr(uc, "bot_github_token", "") or getattr(uc, "github_token", "") or ""
        if not token:
            return "no GitHub token configured"
        read_token = getattr(uc, "github_token", "") or token

        with get_session() as session:
            snap = session.query(Snap).filter_by(id=self.snap_id, user_id=self.user_id).first()
            if not snap:
                return "snap not found"
            snap_name = snap.name
            packaging_repo = snap.packaging_repo

        if not packaging_repo:
            return "no packaging repo configured"

        owner_repo = parse_owner_repo(packaging_repo)
        if not owner_repo:
            return "could not parse packaging repo"
        owner, repo = owner_repo
        owner_repo_str = f"{owner}/{repo}"

        self._report(f"Reviewing stack/dependencies for {owner_repo_str}", snap_name)

        dispatcher = get_coding_dispatcher(uc)
        if not dispatcher:
            return "no coding backend configured/available"

        bot_client = BotGitHubClient(token, bot_login=getattr(uc, "bot_github_login", None), read_token=read_token)
        base_ref = bot_client.get_default_branch(owner, repo)

        prompt = (
            f"This repo ('{snap_name}') is packaged and developed in the same place — "
            "please review it for updates that keep the app current and secure:\n\n"
            "1. Identify the technology stack(s) in use (e.g. Electron/Node.js via "
            "package.json, Rust via Cargo.toml, Python via pyproject.toml/requirements.txt, "
            "Go via go.mod, or more than one of these).\n"
            "2. For each detected stack, check for outdated dependencies and upgrade them "
            "to the latest compatible versions (e.g. `npm outdated`/`cargo outdated`-style "
            "review). Pay particular attention to security-relevant upgrades.\n"
            "3. If this is an Electron app, also check whether the pinned Electron "
            "version is behind the latest stable release and bump it if so — an outdated "
            "Electron ships an outdated, potentially vulnerable bundled Chromium/Node.js. "
            "Note any breaking changes from Electron's release notes in the PR description.\n"
            "4. Run the existing test suite/build if one exists, and fix anything the "
            "upgrade breaks.\n"
            "5. Open a single pull request with all the changes, or leave a comment "
            "explaining what you found if you're not confident making the changes "
            "yourself. Skip opening a PR if everything is already up to date.\n"
        )

        task = dispatcher.start_task(owner, repo, prompt, base_ref=base_ref, create_pull_request=True)
        with get_session() as session:
            session.add(
                CopilotTask(
                    user_id=self.user_id,
                    snap_id=self.snap_id,
                    kind="stack_update",
                    owner_repo=owner_repo_str,
                    prompt=prompt,
                    base_ref=base_ref,
                    **task_result_fields(task, fallback_error=getattr(dispatcher, "last_error", None)),
                )
            )

        return f"dispatched stack update review for {owner_repo_str}" if task else f"dispatch failed for {owner_repo_str}"
