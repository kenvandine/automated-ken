"""Pluggable "coding backend" selection for delegated coding-agent work.

This is the single place that decides *which* backend handles "capable
coding" tasks — fixing failing CI, dependency upgrades, issue fixes, and the
fleet-normalization campaign.

``UserConfig.coding_task_backend`` selects the backend:

- ``local_lemonade`` (default): a single-shot local coding model (Qwen3-Coder,
  run via the embedded/self-managed Lemonade server — see
  ``lemonade/coding_agent.py``). Runs synchronously and opens (or fails to
  open) its PR before ``start_task()`` returns — no polling needed, but it's
  necessarily less capable than a hosted model (no tool use, no running
  tests, no iterating on CI failures).
- ``external_api``: any OpenAI-compatible chat-completions endpoint
  (``external_coding_api_base_url``/``external_coding_api_key``/
  ``external_coding_api_model``). Same synchronous single-shot flow as
  ``local_lemonade`` (see ``lemonade/external_api.py``); token usage is
  recorded under provider ``external`` and shown on ``/stats``.

Callers (``agents/pr_monitor.py``, ``agents/upstream_maintainer.py``,
``agents/repo_normalizer.py``) should always go through
``get_coding_dispatcher()``, and use ``task_result_fields()`` below to turn a
``start_task()`` result into ``CopilotTask`` kwargs (the table name is a
legacy of an earlier backend).
"""

from __future__ import annotations

import logging
import re
from typing import Protocol

logger = logging.getLogger(__name__)

DEFAULT_BACKEND = "local_lemonade"

# CopilotTask statuses that mean the dispatch never produced usable work and
# should NOT block a future retry of the same (kind, owner_repo[, issue])
# combination — used by the various agents' dedup checks (repo_normalizer,
# upstream_maintainer) and by web/routes/copilot_tasks.py to decide whether
# to show a "Retry" button.
RETRY_ELIGIBLE_STATUSES = {"failed", "dispatch_failed", "cancelled", "timed_out"}


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

    Returns ``None`` when the selected backend isn't usable (missing token or
    configuration) — callers should treat that as "skip this dispatch," not
    an error. A legacy ``copilot_cloud_agent`` setting falls back to the
    default backend.
    """
    if uc is None:
        return None
    backend = (getattr(uc, "coding_task_backend", "") or DEFAULT_BACKEND).strip()
    if backend == "copilot_cloud_agent":
        backend = DEFAULT_BACKEND

    token = getattr(uc, "bot_github_token", "") or getattr(uc, "github_token", "") or ""

    if backend == "local_lemonade":
        if not token:
            logger.info("coding_task_backend=local_lemonade but no GitHub token configured")
            return None
        from snap_dashboard.lemonade.coding_agent import LocalLemonadeCodingDispatcher

        return LocalLemonadeCodingDispatcher(uc, token)

    if backend == "external_api":
        if not token:
            logger.info("coding_task_backend=external_api but no GitHub token configured")
            return None
        if not (
            getattr(uc, "external_coding_api_key", "")
            and getattr(uc, "external_coding_api_base_url", "")
            and getattr(uc, "external_coding_api_model", "")
        ):
            logger.info("coding_task_backend=external_api but API key/base URL/model not all configured")
            return None
        from snap_dashboard.lemonade.external_api import ExternalApiCodingDispatcher

        return ExternalApiCodingDispatcher(uc, token)

    logger.warning("unknown coding_task_backend %r — skipping dispatch", backend)
    return None


def extract_pr_url(remote: dict) -> str | None:
    """Best-effort PR URL extraction from a coding-backend task result."""
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

    Handles a top-level ``{"number": ...}``, a nested ``pull_request`` dict,
    and falls back to parsing the trailing digits off a ``.../pull/123`` URL.
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
        m = re.search(r"/pull/(\d+)/?$", url)
        if m:
            return int(m.group(1))
    return None


def task_result_fields(task: dict | None, fallback_error: str | None = None) -> dict:
    """Turn a ``CodingDispatcher.start_task()`` result into ``CopilotTask`` kwargs.

    Backends do all their work synchronously inside ``start_task()``, so the
    result already has a terminal ``state`` ("completed"/"failed") and, on
    success, a PR url; on failure it carries an ``error`` string.

    ``task is None`` means the dispatch call never got a result to interpret
    — ``fallback_error`` fills in ``error_msg`` for that case, so a
    ``dispatch_failed`` row isn't left with no explanation at all.

    Returns a dict with ``external_task_id``, ``status``, ``pr_url``, and
    ``error_msg``, suitable for ``**``-splatting into a ``CopilotTask(...)``
    constructor.
    """
    if not task:
        return {
            "external_task_id": None,
            "status": "dispatch_failed",
            "pr_url": None,
            "error_msg": fallback_error or "Dispatch failed with no further details — see server logs.",
        }
    state = task.get("state")
    if state:
        return {
            "external_task_id": None,
            "status": state,
            "pr_url": extract_pr_url(task),
            "error_msg": task.get("error") if state == "failed" else None,
        }
    task_id = task.get("id")
    return {
        "external_task_id": str(task_id) if task_id is not None else None,
        "status": "queued",
        "pr_url": None,
        "error_msg": None,
    }
