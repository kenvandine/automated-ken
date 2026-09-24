"""Upstream maintainer agent — routine maintenance for repos the user is upstream for.

Some tracked snaps package software the user personally develops (e.g. an
Electron app). For those repos this agent, opt-in via
``UserConfig.auto_maintain_upstream``:

  1. Asks Copilot cloud agent to check for outdated npm/node dependencies
     and open an upgrade PR (rate-limited to at most one dispatch per repo
     per ``_DEP_UPDATE_COOLDOWN_DAYS``).
  2. Requests a Copilot code review on any of the user's own open PRs that
     doesn't have one yet.
  3. Triages open issues and, for a small capped batch per run, asks
     Copilot cloud agent to attempt a fix (each issue is only ever
     dispatched once — tracked via CopilotTask.issue_number).

All actual code-writing is delegated to Copilot cloud agent (the "capable
coding" model tier) rather than attempted locally — see github/copilot_agent.py.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

from snap_dashboard.agents.base import BaseAgent
from snap_dashboard.agents.coding_backend import CodingDispatcher, get_coding_dispatcher, task_result_fields
from snap_dashboard.auth import get_user_config
from snap_dashboard.db.models import CopilotTask, Snap, User
from snap_dashboard.db.session import get_session
from snap_dashboard.github.copilot_agent import CopilotAgentClient
from snap_dashboard.github.utils import parse_owner_repo

logger = logging.getLogger(__name__)

_GH_API = "https://api.github.com"
_DEP_UPDATE_COOLDOWN_DAYS = 14
_MAX_ISSUES_PER_RUN = 3


def _gh_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }


class UpstreamMaintainerAgent(BaseAgent):
    """Routine dependency/issue/PR-review maintenance for repos the user owns upstream."""

    agent_type = "upstream_maintainer"

    def __init__(self, user_id: int | None = None) -> None:
        super().__init__(user_id=user_id)

    def _run(self) -> str:
        if not self.user_id:
            return "no user_id — skipped"
        uc = get_user_config(self.user_id)
        if not uc or not getattr(uc, "auto_maintain_upstream", False):
            return "disabled"
        token = (getattr(uc, "bot_github_token", "") or getattr(uc, "github_token", "") or "")
        if not token:
            return "no GitHub token configured"

        with get_session() as session:
            user = session.query(User).get(self.user_id)
            login = (user.github_login or "").lower() if user else ""
            snaps = session.query(Snap).filter_by(user_id=self.user_id).all()
            upstream_repos = [
                (s.id, s.name, s.upstream_repo)
                for s in snaps
                if s.upstream_repo and self._owned_by(s.upstream_repo, login)
            ]

        if not upstream_repos:
            return "no upstream-owned repos found"

        client = get_coding_dispatcher(uc)
        if not client:
            return "no coding backend configured/available"
        actions = []
        for snap_id, snap_name, upstream_repo in upstream_repos:
            owner_repo = parse_owner_repo(upstream_repo)
            if not owner_repo:
                continue
            owner, repo = owner_repo
            self._report(f"Maintaining {upstream_repo}", snap_name)
            actions.append(self._maybe_dep_update(client, snap_id, owner, repo, token))
            actions.append(self._maybe_request_reviews(client, owner, repo, token))
            actions.append(self._maybe_triage_issues(client, snap_id, owner, repo, token))

        done = [a for a in actions if a]
        return f"checked {len(upstream_repos)} upstream repo(s), {len(done)} action(s) taken"

    @staticmethod
    def _owned_by(upstream_repo: str, login: str) -> bool:
        owner_repo = parse_owner_repo(upstream_repo)
        if not owner_repo or not login:
            return False
        return owner_repo[0].lower() == login

    # ------------------------------------------------------------------
    # Dependency updates
    # ------------------------------------------------------------------

    def _maybe_dep_update(self, client: CodingDispatcher, snap_id: int, owner: str, repo: str, token: str) -> bool:
        owner_repo = f"{owner}/{repo}"
        cutoff = datetime.now(timezone.utc) - timedelta(days=_DEP_UPDATE_COOLDOWN_DAYS)
        with get_session() as session:
            recent = (
                session.query(CopilotTask)
                .filter(
                    CopilotTask.kind == "dep_update",
                    CopilotTask.owner_repo == owner_repo,
                    CopilotTask.created_at >= cutoff,
                )
                .first()
            )
            if recent:
                return False

        prompt = (
            f"Check {owner_repo} for outdated npm/node dependencies (package.json / "
            "package-lock.json). If any are outdated, upgrade them to the latest "
            "compatible versions, run the existing test suite if one exists, and open "
            "a pull request with the dependency bumps. Skip if everything is already "
            "up to date — don't open an empty PR."
        )
        task = client.start_task(owner, repo, prompt, base_ref="main", create_pull_request=True)
        with get_session() as session:
            session.add(
                CopilotTask(
                    user_id=self.user_id,
                    snap_id=snap_id,
                    kind="dep_update",
                    owner_repo=owner_repo,
                    prompt=prompt,
                    **task_result_fields(task),
                )
            )
        return bool(task)

    # ------------------------------------------------------------------
    # PR review requests
    # ------------------------------------------------------------------

    def _maybe_request_reviews(self, client: CodingDispatcher, owner: str, repo: str, token: str) -> bool:
        try:
            with httpx.Client(timeout=15) as http:
                resp = http.get(
                    f"{_GH_API}/repos/{owner}/{repo}/pulls",
                    params={"state": "open", "per_page": 20},
                    headers=_gh_headers(token),
                )
            if resp.status_code != 200:
                return False
            prs = resp.json()
        except httpx.HTTPError as exc:
            logger.warning("upstream_maintainer: list PRs failed for %s/%s: %s", owner, repo, exc)
            return False

        acted = False
        for pr in prs:
            reviewers = [r.get("login", "") for r in pr.get("requested_reviewers", [])]
            already_reviewed = any("copilot" in (r or "").lower() for r in reviewers)
            if already_reviewed:
                continue
            # request_pr_review is a Copilot-cloud-agent-specific feature (it
            # requests the "copilot-pull-request-reviewer[bot]" reviewer),
            # not part of the generic CodingDispatcher protocol — skip it
            # cleanly for any other backend.
            if not isinstance(client, CopilotAgentClient):
                continue
            if client.request_pr_review(owner, repo, pr["number"]):
                acted = True
        return acted

    # ------------------------------------------------------------------
    # Issue triage / auto-fix attempts
    # ------------------------------------------------------------------

    def _maybe_triage_issues(self, client: CodingDispatcher, snap_id: int, owner: str, repo: str, token: str) -> bool:
        owner_repo = f"{owner}/{repo}"
        try:
            with httpx.Client(timeout=15) as http:
                resp = http.get(
                    f"{_GH_API}/repos/{owner}/{repo}/issues",
                    params={"state": "open", "per_page": 30},
                    headers=_gh_headers(token),
                )
            if resp.status_code != 200:
                return False
            issues = [i for i in resp.json() if "pull_request" not in i]
        except httpx.HTTPError as exc:
            logger.warning("upstream_maintainer: list issues failed for %s/%s: %s", owner, repo, exc)
            return False

        if not issues:
            return False

        with get_session() as session:
            already_dispatched = {
                t.issue_number
                for t in session.query(CopilotTask)
                .filter(CopilotTask.kind == "issue_fix", CopilotTask.owner_repo == owner_repo)
                .all()
            }

        dispatched = 0
        acted = False
        for issue in issues:
            if dispatched >= _MAX_ISSUES_PER_RUN:
                break
            number = issue["number"]
            if number in already_dispatched:
                continue
            prompt = (
                f"Attempt to fix issue #{number} in {owner_repo}: \"{issue.get('title', '')}\".\n\n"
                f"{(issue.get('body') or '')[:4000]}\n\n"
                "If you can confidently resolve this, open a pull request with the fix "
                "and reference the issue. If the issue is unclear, needs a design "
                "decision, or you're not confident in a safe fix, leave a comment "
                "explaining what's needed instead of guessing."
            )
            task = client.start_task(owner, repo, prompt, base_ref="main", create_pull_request=True)
            with get_session() as session:
                session.add(
                    CopilotTask(
                        user_id=self.user_id,
                        snap_id=snap_id,
                        kind="issue_fix",
                        owner_repo=owner_repo,
                        prompt=prompt,
                        issue_number=number,
                        **task_result_fields(task),
                    )
                )
            dispatched += 1
            acted = acted or bool(task)
        return acted
