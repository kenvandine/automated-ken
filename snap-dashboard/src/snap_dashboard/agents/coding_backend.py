"""Pluggable "coding backend" selection for delegated coding-agent work.

This is the single place that decides *which* backend handles "capable
coding" tasks — fixing failing CI, dependency upgrades, issue fixes, and the
fleet-normalization campaign. The long-term goal is to run as much of this
locally as capable local models become available; today, no local model in
this project (the embedded Lemonade vision/text model) is capable of
autonomous multi-file code editing, so **GitHub Copilot cloud agent** is the
practical default — it needs no extra credentials (reuses the existing bot
GitHub token) and is strictly more capable for this class of work.

``UserConfig.coding_task_backend`` selects the backend:

- ``copilot_cloud_agent`` (default): dispatch to GitHub Copilot cloud agent.
- ``local_lemonade``: reserved for when a local model is capable enough for
  this. Not implemented yet — returns ``None`` (skip dispatch) with a clear
  log message rather than pretending to work.
- ``external_api``: reserved for a user-supplied API key to a hosted coding
  model (``external_coding_api_key``/``external_coding_api_base_url``/
  ``external_coding_api_model``). Not implemented yet, same as above — this
  is the forward-looking "add an API key to offload some work to cloud
  models" escalation path the user asked for.

Callers (``agents/pr_monitor.py``, ``agents/upstream_maintainer.py``,
``agents/repo_normalizer.py``) should always go through
``get_coding_dispatcher()`` rather than constructing ``CopilotAgentClient``
directly, so a future local/external backend only needs to be added here.
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
        logger.info(
            "coding_task_backend=local_lemonade selected, but no local model is "
            "currently capable of autonomous code-editing tasks — skipping dispatch. "
            "This is a reserved extension point for future local models."
        )
        return None

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
