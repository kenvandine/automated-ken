"""Issue/PR review agent — "what needs my attention right now" for one snap.

Triggered manually from the snap detail page's "Review Issues & PRs"
button. Pulls every open issue and PR from the snap's packaging repo (and
its upstream repo too, if it's a separate one), summarizes what needs
attention in plain English, and stores the result (``IssueReviewReport``)
so the page can show it plus a per-item "Address with Copilot" action —
reusing the same ``issue_fix``-style dispatch ``UpstreamMaintainerAgent``
uses for its own automatic issue triage, just user-initiated and covering
PRs too (requesting a Copilot review) rather than only issues.

Summarization prefers a local Lemonade text model (cheap, no external
dispatch) when one is configured/available; falls back to a deterministic
heuristic summary (oldest-first, PRs without reviews flagged, issues with
lots of engagement flagged) if not, so the feature still works with zero
extra setup.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import httpx

from snap_dashboard.agents.base import BaseAgent
from snap_dashboard.agents.coding_backend import (
    RETRY_ELIGIBLE_STATUSES,
    get_coding_dispatcher,
    task_result_fields,
)
from snap_dashboard.auth import get_user_config
from snap_dashboard.db.models import CopilotTask, IssueReviewReport, Snap
from snap_dashboard.db.session import get_session
from snap_dashboard.github.bot_client import BotGitHubClient
from snap_dashboard.github.copilot_agent import CopilotAgentClient
from snap_dashboard.github.utils import parse_owner_repo, parse_repo_slug

logger = logging.getLogger(__name__)

_GH_API = "https://api.github.com"
# Cap how many open issues/PRs get pulled per repo — this is a "what needs
# attention" triage aid, not an exhaustive audit, and keeps the summary
# prompt (and the rendered table) a manageable size.
_MAX_ITEMS_PER_REPO = 25
# Items idle this long without a fresh look get flagged as "stale" in the
# heuristic (no-LLM) summary path.
_STALE_DAYS = 30


def _gh_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }


class IssuePrReviewAgent(BaseAgent):
    """One-off "summarize open issues/PRs and flag what needs attention" run."""

    agent_type = "issue_pr_reviewer"

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

        with get_session() as session:
            snap = session.query(Snap).filter_by(id=self.snap_id, user_id=self.user_id).first()
            if not snap:
                return "snap not found"
            snap_name = snap.name
            repos = {
                r
                for r in (snap.packaging_repo, snap.upstream_repo)
                if r and parse_owner_repo(r)
            }

        if not repos:
            self._save_report(summary="", items=[], error_msg="No packaging or upstream repo configured.")
            return "no repos configured"

        self._report(f"Reviewing open issues/PRs for {snap_name}", snap_name)

        items: list[dict] = []
        for repo_url in sorted(repos, key=parse_repo_slug):
            owner, repo = parse_owner_repo(repo_url)
            items.extend(self._fetch_repo_items(owner, repo, token))

        if not items:
            self._save_report(summary="No open issues or pull requests found.", items=[])
            return f"{snap_name}: nothing open"

        summary = self._summarize(uc, snap_name, items)
        self._save_report(summary=summary, items=items)
        return f"{snap_name}: reviewed {len(items)} open item(s)"

    # ------------------------------------------------------------------
    # GitHub fetch
    # ------------------------------------------------------------------

    def _fetch_repo_items(self, owner: str, repo: str, token: str) -> list[dict]:
        owner_repo = f"{owner}/{repo}"
        try:
            with httpx.Client(timeout=15) as http:
                resp = http.get(
                    f"{_GH_API}/repos/{owner}/{repo}/issues",
                    params={"state": "open", "per_page": _MAX_ITEMS_PER_REPO, "sort": "created", "direction": "asc"},
                    headers=_gh_headers(token),
                )
            if resp.status_code != 200:
                logger.warning("issue_pr_reviewer: list issues failed for %s (%s)", owner_repo, resp.status_code)
                return []
            raw = resp.json()
        except httpx.HTTPError as exc:
            logger.warning("issue_pr_reviewer: list issues failed for %s: %s", owner_repo, exc)
            return []

        now = datetime.now(timezone.utc)
        out = []
        for entry in raw:
            is_pr = "pull_request" in entry
            created_at = entry.get("created_at")
            age_days = None
            if created_at:
                try:
                    created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    age_days = (now - created).days
                except ValueError:
                    age_days = None
            out.append(
                {
                    "owner_repo": owner_repo,
                    "number": entry.get("number"),
                    "type": "pr" if is_pr else "issue",
                    "title": entry.get("title") or "",
                    "url": entry.get("html_url") or "",
                    "author": (entry.get("user") or {}).get("login") or "",
                    "age_days": age_days,
                    "comments": entry.get("comments") or 0,
                    "body": (entry.get("body") or "")[:1500],
                }
            )
        return out[:_MAX_ITEMS_PER_REPO]

    # ------------------------------------------------------------------
    # Summarization
    # ------------------------------------------------------------------

    def _summarize(self, uc, snap_name: str, items: list[dict]) -> str:
        lemonade = self._get_lemonade(uc, task="text")
        if lemonade:
            llm_summary = self._summarize_with_llm(lemonade, snap_name, items)
            if llm_summary:
                return llm_summary
            # Lemonade *was* configured/reachable but the chat call itself
            # failed or returned nothing — a different situation from "not
            # configured at all", so don't tell the user to go configure
            # something they already have. See _summarize_with_llm's own
            # logger.warning (in lemonade.chat) for the actual failure
            # reason (timeout, non-200, cold model load, etc).
            return self._summarize_heuristic(items, lemonade_failed=True)
        return self._summarize_heuristic(items, lemonade_failed=False)

    def _summarize_with_llm(self, lemonade, snap_name: str, items: list[dict]) -> str | None:
        listing = "\n".join(
            f"- [{it['type'].upper()} #{it['number']}] \"{it['title']}\" "
            f"(repo {it['owner_repo']}, {it['age_days'] if it['age_days'] is not None else '?'} days old, "
            f"{it['comments']} comment(s)): {it['body'][:300]}"
            for it in items
        )
        prompt = (
            f"Here are the open GitHub issues and pull requests for the snap '{snap_name}':\n\n"
            f"{listing}\n\n"
            "Write a short plain-English summary (a few sentences, then a short bulleted list) "
            "of what most needs attention — e.g. old/stale items, PRs waiting on review or CI, "
            "issues reporting real bugs vs. questions/feature requests. Be specific about which "
            "issue/PR numbers matter most and why."
        )
        summary = lemonade.chat(prompt, temperature=0.2, max_tokens=500)
        if not summary:
            # lemonade.chat() already logs the specific httpx/HTTP-status
            # reason at warning level; add snap context here so it's easy
            # to correlate with the "heuristic summary" fallback the user sees.
            logger.warning(
                "issue_pr_reviewer: Lemonade summarization returned nothing for %s "
                "(%d item(s)) — falling back to heuristic summary", snap_name, len(items),
            )
            return None
        return summary.strip()

    @staticmethod
    def _summarize_heuristic(items: list[dict], lemonade_failed: bool = False) -> str:
        issues = [i for i in items if i["type"] == "issue"]
        prs = [i for i in items if i["type"] == "pr"]
        stale = [i for i in items if (i["age_days"] or 0) >= _STALE_DAYS]
        lines = [f"{len(issues)} open issue(s), {len(prs)} open pull request(s)."]
        if stale:
            lines.append(
                f"{len(stale)} item(s) have been open {_STALE_DAYS}+ days and may need a fresh look:"
            )
            for it in sorted(stale, key=lambda i: -(i["age_days"] or 0))[:8]:
                lines.append(f"  - [{it['type'].upper()} #{it['number']}] {it['title']} ({it['age_days']}d old)")
        if lemonade_failed:
            # Distinct from "not configured": a model *is* set up and the
            # server was reachable, but the summarization request itself
            # failed (timed out, cold model load took too long, non-200
            # response, etc) — see the "issue_pr_reviewer"/"lemonade chat"
            # warnings in the server log for the specific reason.
            no_llm_note = (
                "\n(The local Lemonade text model is configured but the summarization "
                "request failed or timed out — this is a heuristic summary instead. "
                "Check the server log for a 'lemonade chat' warning for details; a cold "
                "model load can take a while on first use, so retrying may help.)"
            )
        else:
            no_llm_note = (
                "\n(No local text model configured — this is a heuristic summary. "
                "Configure a Lemonade model in Settings for a richer plain-English summary.)"
            )
        return "\n".join(lines) + no_llm_note

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save_report(self, summary: str, items: list[dict], error_msg: str | None = None) -> None:
        with get_session() as session:
            existing = session.query(IssueReviewReport).filter_by(snap_id=self.snap_id).first()
            if existing:
                existing.user_id = self.user_id
                existing.summary = summary
                existing.items_json = json.dumps(items)
                existing.error_msg = error_msg
                existing.updated_at = datetime.now(timezone.utc)
            else:
                session.add(
                    IssueReviewReport(
                        user_id=self.user_id,
                        snap_id=self.snap_id,
                        summary=summary,
                        items_json=json.dumps(items),
                        error_msg=error_msg,
                    )
                )


class AddressReviewItemAgent(BaseAgent):
    """"Address with Copilot" action for a single item flagged by ``IssuePrReviewAgent``.

    An issue gets the same ``issue_fix`` treatment ``UpstreamMaintainerAgent``
    dispatches automatically for repos the user maintains upstream — an
    attempted fix PR, or a comment if Copilot isn't confident. A PR instead
    gets a Copilot review request (there's no code for Copilot to write
    against someone else's open PR, just feedback to give) — only meaningful
    when the configured coding backend is the Copilot cloud agent itself,
    since ``request_pr_review`` is a Copilot-cloud-agent-specific GitHub
    reviewer-request API, not part of the generic ``CodingDispatcher``
    protocol other backends implement.
    """

    agent_type = "review_item_address"

    def __init__(
        self,
        user_id: int | None = None,
        snap_id: int | None = None,
        owner_repo: str = "",
        number: int = 0,
        item_type: str = "issue",
        title: str = "",
        body: str = "",
    ) -> None:
        super().__init__(user_id=user_id)
        self.snap_id = snap_id
        self.owner_repo = owner_repo
        self.number = number
        self.item_type = item_type
        self.title = title
        self.body = body

    def _run(self) -> str:
        if not self.user_id or not self.owner_repo or not self.number:
            return "missing required fields — skipped"
        uc = get_user_config(self.user_id)
        if not uc:
            return "no user config"
        token = getattr(uc, "bot_github_token", "") or getattr(uc, "github_token", "") or ""
        if not token:
            return "no GitHub token configured"
        owner_repo_parts = parse_owner_repo(self.owner_repo)
        if not owner_repo_parts:
            return "could not parse owner/repo"
        owner, repo = owner_repo_parts
        owner_repo_str = f"{owner}/{repo}"

        dispatcher = get_coding_dispatcher(uc)
        if not dispatcher:
            return "no coding backend configured/available"

        if self.item_type == "pr":
            if not isinstance(dispatcher, CopilotAgentClient) and not hasattr(dispatcher, "_copilot"):
                return "PR review requests need the Copilot cloud agent backend"
            copilot = dispatcher if isinstance(dispatcher, CopilotAgentClient) else getattr(dispatcher, "_copilot")
            ok = copilot.request_pr_review(owner, repo, self.number)
            with get_session() as session:
                session.add(
                    CopilotTask(
                        user_id=self.user_id,
                        snap_id=self.snap_id,
                        kind="pr_review_request",
                        owner_repo=owner_repo_str,
                        issue_number=self.number,
                        status="requested" if ok else "dispatch_failed",
                        error_msg=None if ok else "Failed to request a Copilot review — see server logs.",
                    )
                )
            return f"requested Copilot review on {owner_repo_str}#{self.number}" if ok else "review request failed"

        with get_session() as session:
            existing = (
                session.query(CopilotTask)
                .filter(
                    CopilotTask.kind == "issue_fix",
                    CopilotTask.owner_repo == owner_repo_str,
                    CopilotTask.issue_number == self.number,
                )
                .order_by(CopilotTask.id.desc())
                .first()
            )
            if existing and existing.status not in RETRY_ELIGIBLE_STATUSES:
                return f"{owner_repo_str}#{self.number}: already dispatched"

        read_token = getattr(uc, "github_token", "") or token
        base_ref = BotGitHubClient(token, read_token=read_token).get_default_branch(owner, repo)
        prompt = (
            f"Attempt to fix issue #{self.number} in {owner_repo_str}: \"{self.title}\".\n\n"
            f"{self.body[:4000]}\n\n"
            "If you can confidently resolve this, open a pull request with the fix "
            "and reference the issue. If the issue is unclear, needs a design "
            "decision, or you're not confident in a safe fix, leave a comment "
            "explaining what's needed instead of guessing."
        )
        task = dispatcher.start_task(owner, repo, prompt, base_ref=base_ref, create_pull_request=True)
        with get_session() as session:
            session.add(
                CopilotTask(
                    user_id=self.user_id,
                    snap_id=self.snap_id,
                    kind="issue_fix",
                    owner_repo=owner_repo_str,
                    prompt=prompt,
                    issue_number=self.number,
                    base_ref=base_ref,
                    **task_result_fields(task, fallback_error=getattr(dispatcher, "last_error", None)),
                )
            )
        return f"dispatched issue_fix for {owner_repo_str}#{self.number}" if task else "dispatch failed"
