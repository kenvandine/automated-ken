"""Client for GitHub Copilot cloud agent — the "capable coding" model tier.

Rather than reinventing code-fixing with a local/small LLM, genuinely
capable coding work (fixing a failing CI workflow, upgrading node deps,
attempting a fix for a filed issue) is delegated to Copilot cloud agent via
its REST API. This needs a *user-to-server* token (a classic/fine-grained
PAT or OAuth token — NOT a GitHub App installation token), which is exactly
what ``UserConfig.bot_github_token``/``github_token`` already are.

See: https://docs.github.com/en/copilot/how-tos/use-copilot-agents/cloud-agent/use-cloud-agent-via-the-api
"""

from __future__ import annotations

import logging

import httpx

from snap_dashboard.telemetry import estimate_tokens, record_model_usage

logger = logging.getLogger(__name__)

_GH_API = "https://api.github.com"

# Bot login that reviews PRs when requested as a reviewer.
COPILOT_REVIEWER_LOGIN = "copilot-pull-request-reviewer[bot]"


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }


class CopilotAgentClient:
    """Starts and tracks Copilot cloud agent tasks, and requests Copilot PR reviews."""

    def __init__(self, token: str) -> None:
        self.token = token

    def start_task(
        self,
        owner: str,
        repo: str,
        prompt: str,
        base_ref: str = "main",
        create_pull_request: bool = True,
        model: str | None = None,
    ) -> dict | None:
        """POST /agents/repos/{owner}/{repo}/tasks — returns the task dict or None."""
        payload: dict = {
            "prompt": prompt,
            "base_ref": base_ref,
            "create_pull_request": create_pull_request,
        }
        if model:
            payload["model"] = model
        try:
            with httpx.Client(timeout=30) as client:
                resp = client.post(
                    f"{_GH_API}/agents/repos/{owner}/{repo}/tasks",
                    json=payload,
                    headers=_headers(self.token),
                )
            if resp.status_code in (200, 201, 202):
                # Copilot's cloud-agent task API never reports token usage —
                # the agent's work happens out-of-band and is only polled
                # for status/PR-url later (see get_task()) — so this is a
                # rough, input-only estimate of the dispatched prompt size,
                # just to surface *some* signal for how much cloud work is
                # being done vs. local (see snap_dashboard.telemetry).
                record_model_usage(
                    provider="copilot",
                    model=model or "copilot-default",
                    task="coding_agent",
                    input_tokens=estimate_tokens(prompt),
                    output_tokens=0,
                    estimated=True,
                )
                return resp.json()
            logger.warning(
                "copilot start_task failed %s/%s (%s): %s",
                owner, repo, resp.status_code, resp.text[:300],
            )
        except httpx.HTTPError as exc:
            logger.warning("copilot start_task failed for %s/%s: %s", owner, repo, exc)
        return None

    def get_task(self, owner: str, repo: str, task_id: str) -> dict | None:
        """GET /agents/repos/{owner}/{repo}/tasks/{task_id} — returns the task dict or None."""
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.get(
                    f"{_GH_API}/agents/repos/{owner}/{repo}/tasks/{task_id}",
                    headers=_headers(self.token),
                )
            if resp.status_code == 200:
                return resp.json()
        except httpx.HTTPError as exc:
            logger.warning("copilot get_task failed for %s/%s#%s: %s", owner, repo, task_id, exc)
        return None

    def request_pr_review(self, owner: str, repo: str, pr_number: int) -> bool:
        """Request a Copilot code review on an existing PR.

        Uses the same "requested reviewers" endpoint a human reviewer request
        would use, with Copilot's review-bot login.
        """
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.post(
                    f"{_GH_API}/repos/{owner}/{repo}/pulls/{pr_number}/requested_reviewers",
                    json={"reviewers": [COPILOT_REVIEWER_LOGIN]},
                    headers=_headers(self.token),
                )
            return resp.status_code in (200, 201)
        except httpx.HTTPError as exc:
            logger.warning("copilot request_pr_review failed for %s/%s#%s: %s", owner, repo, pr_number, exc)
            return False
