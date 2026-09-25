"""Pluggable "coding backend" selection for delegated coding-agent work.

This is the single place that decides *which* backend handles "capable
coding" tasks — fixing failing CI, dependency upgrades, issue fixes, and the
fleet-normalization campaign.

``UserConfig.coding_task_backend`` selects the backend:

- ``copilot_cloud_agent`` (default): dispatch to GitHub Copilot cloud agent
  — an async task, polled later for a PR link (see ``get_task()`` in
  ``github/copilot_agent.py`` and ``web/routes/copilot_tasks.py``). Needs no
  extra credentials (reuses the existing bot GitHub token).
- ``local_lemonade``: a single-shot local coding model (Qwen3-Coder, run via
  the embedded/self-managed Lemonade server — see ``lemonade/coding_agent.py``).
  Runs synchronously and opens (or fails to open) its PR before
  ``start_task()`` returns — no polling needed, but it's necessarily less
  capable than the cloud agent (no tool use, no running tests, no iterating
  on CI failures).
- ``external_api``: reserved for a user-supplied API key to a hosted coding
  model (``external_coding_api_key``/``external_coding_api_base_url``/
  ``external_coding_api_model``). Not implemented yet — this is the
  forward-looking "add an API key to offload some work to cloud models"
  escalation path the user asked for.

Callers (``agents/pr_monitor.py``, ``agents/upstream_maintainer.py``,
``agents/repo_normalizer.py``) should always go through
``get_coding_dispatcher()`` rather than constructing ``CopilotAgentClient``
directly, so a future local/external backend only needs to be added here.
They should also use ``task_result_fields()`` below to turn a
``start_task()`` result into ``CopilotTask`` kwargs, since the two
implemented backends return different result shapes (async id-to-poll vs.
already-finished state+PR url).
"""

from __future__ import annotations

import logging
from typing import Protocol

from snap_dashboard.github.copilot_agent import CopilotAgentClient

logger = logging.getLogger(__name__)


class CodingDispatcher(Protocol):
    """Common interface every coding backend must expose."""

    def start_task(
        self,
        owner: str,
        repo: str,
        prompt: str,
        base_ref: str = "main",
        create_pull_request: bool = True,
        model: str | None = None,
    ) -> dict | None: ...


def get_coding_dispatcher(uc) -> CodingDispatcher | None:
    """Return the configured coding-task dispatcher for this user, or None.

    Returns ``None`` when the selected backend isn't usable (missing token,
    or a backend that's a reserved-but-not-yet-implemented placeholder) —
    callers should treat that as "skip this dispatch," not an error.
    """
    if uc is None:
        return None
    backend = (getattr(uc, "coding_task_backend", "") or "copilot_cloud_agent").strip()

    if backend == "copilot_cloud_agent":
        token = getattr(uc, "bot_github_token", "") or getattr(uc, "github_token", "") or ""
        if not token:
            logger.info("coding_task_backend=copilot_cloud_agent but no GitHub token configured")
            return None
        return CopilotAgentClient(token)

    if backend == "local_lemonade":
        token = getattr(uc, "bot_github_token", "") or getattr(uc, "github_token", "") or ""
        if not token:
            logger.info("coding_task_backend=local_lemonade but no GitHub token configured")
            return None
        from snap_dashboard.lemonade.coding_agent import LocalLemonadeCodingDispatcher

        return LocalLemonadeCodingDispatcher(uc, token)

    if backend == "external_api":
        api_key = getattr(uc, "external_coding_api_key", "") or ""
        if not api_key:
            logger.info("coding_task_backend=external_api but no external_coding_api_key configured")
            return None
        logger.info(
            "coding_task_backend=external_api is not implemented yet — skipping dispatch. "
            "This is a reserved extension point for a user-supplied hosted coding model."
        )
        return None

    logger.warning("unknown coding_task_backend %r — skipping dispatch", backend)
    return None


def extract_pr_url(remote: dict) -> str | None:
    """Best-effort PR URL extraction from a coding-backend task result.

    Handles both the GitHub Copilot cloud agent task-status shape (the
    agent-tasks API is documented as "public preview and subject to change"
    with no fixed schema for the pull-request field) and the local Lemonade
    backend's own ``{"html_url": ...}`` shape.
    """
    for key in ("pull_request_url", "html_url"):
        val = remote.get(key)
        if isinstance(val, str) and val:
            return val
    pr = remote.get("pull_request")
    if isinstance(pr, dict):
        return pr.get("html_url") or pr.get("url")
    if isinstance(pr, str) and pr:
        return pr
    return None


def extract_pr_number(remote: dict) -> int | None:
    """Best-effort PR number extraction from a coding-backend task result.

    Handles the local Lemonade backend's own ``{"number": ...}`` shape, a
    nested ``pull_request`` dict (either shape), and falls back to parsing
    the trailing digits off a ``.../pull/123`` URL when nothing else works.
    """
    val = remote.get("number")
    if isinstance(val, int):
        return val
    pr = remote.get("pull_request")
    if isinstance(pr, dict):
        num = pr.get("number")
        if isinstance(num, int):
            return num
    url = extract_pr_url(remote)
    if url:
        import re

        m = re.search(r"/pull/(\d+)/?$", url)
        if m:
            return int(m.group(1))
    return None


def task_result_fields(task: dict | None) -> dict:
    """Turn a ``CodingDispatcher.start_task()`` result into ``CopilotTask`` kwargs.

    The two implemented backends return different result shapes:

    - GitHub Copilot cloud agent dispatches an async remote task — the
      result only has an ``id`` to poll later (see
      ``web/routes/copilot_tasks.py``), so the initial status is ``queued``
      with no PR url yet.
    - The local Lemonade backend does all its work synchronously inside
      ``start_task()`` itself, so the result already has a terminal
      ``state`` ("completed"/"failed") and, on success, a PR url — nothing
      left to poll, and ``external_task_id`` stays unset.

    Returns a dict with ``external_task_id``, ``status``, and ``pr_url``,
    suitable for ``**``-splatting into a ``CopilotTask(...)`` constructor.
    """
    if not task:
        return {"external_task_id": None, "status": "dispatch_failed", "pr_url": None}
    state = task.get("state")
    if state:
        return {"external_task_id": None, "status": state, "pr_url": extract_pr_url(task)}
    task_id = task.get("id")
    return {
        "external_task_id": str(task_id) if task_id is not None else None,
        "status": "queued",
        "pr_url": None,
    }
