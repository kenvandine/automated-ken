"""Best-effort LLM token-usage tracking, local vs. cloud.

Every LLM call in the system — local models via lemonade-server
(``snap_dashboard.lemonade.client.LemonadeClient``) and, when configured,
GitHub Copilot cloud agent (``snap_dashboard.github.copilot_agent.
CopilotAgentClient``) — records an input/output token count here via
``record_model_usage()``. Counts are exact when the backend's own API
reports real usage; otherwise ``estimate_tokens()`` provides a rough
model-agnostic fallback. The ``/stats`` page aggregates these rows by
provider and model so a user can see how much of the platform's AI work is
being handled locally vs. offloaded to the cloud.

Recording is always best-effort and must never raise — a failure here must
never break the LLM call it's instrumenting.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Rough, model-agnostic heuristic for text lacking a real token count from
# the backend's own API: ~4 characters per token is the commonly-cited
# average for English text across most tokenizers.
_CHARS_PER_TOKEN = 4

# Vision models spend a meaningful, fairly constant number of tokens per
# encoded image regardless of the accompanying text; used only when the
# backend doesn't report real usage, to keep vision-call estimates from
# looking artificially cheap.
ESTIMATED_TOKENS_PER_IMAGE = 800


def estimate_tokens(text: str | None) -> int:
    """Rough token-count estimate (~4 chars/token) for text with no real usage data.

    Returns 0 for empty/None input, otherwise at least 1.
    """
    if not text:
        return 0
    return max(1, len(text) // _CHARS_PER_TOKEN)


def record_model_usage(
    provider: str,
    model: str,
    task: str,
    input_tokens: int,
    output_tokens: int,
    estimated: bool = True,
) -> None:
    """Persist one LLM call's token usage. Never raises.

    ``provider`` is "lemonade" (local) or "copilot" (cloud). ``task`` is a
    short label for the kind of call (e.g. "chat", "vision_compare",
    "coding_agent"). ``estimated`` marks whether the counts are a heuristic
    guess (see ``estimate_tokens()``) rather than a real count reported by
    the backend's own API.
    """
    from snap_dashboard.db.models import ModelUsage
    from snap_dashboard.db.session import get_session

    try:
        with get_session() as session:
            session.add(
                ModelUsage(
                    provider=provider,
                    model=model or "unknown",
                    task=task,
                    input_tokens=max(0, int(input_tokens or 0)),
                    output_tokens=max(0, int(output_tokens or 0)),
                    estimated=estimated,
                )
            )
    except Exception:
        logger.debug(
            "failed to record model usage (%s/%s/%s)", provider, model, task, exc_info=True
        )
