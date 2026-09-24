"""Lemonade-server client — OpenAI-compatible local LLM endpoint.

Connects to a locally-running lemonade-server instance that exposes the
OpenAI chat completions API.  All calls degrade gracefully: if the server
is not reachable the methods return ``None`` and the caller falls back to
heuristics.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import httpx

from snap_dashboard.telemetry import (
    ESTIMATED_TOKENS_PER_IMAGE,
    estimate_tokens,
    record_model_usage,
)

logger = logging.getLogger(__name__)

_DEFAULT_URL = "http://localhost:13305"
_DEFAULT_MODEL = "user.Qwen3.5-35B-A3B-Q4_K_M"
# Generation on CPU/iGPU can be slow, and a not-yet-pulled model triggers a
# multi-gigabyte on-demand download before the first token — give both
# plenty of headroom rather than failing a cold first request.
_TIMEOUT = 600


class LemonadeClient:
    """Thin client for lemonade-server's OpenAI-compatible API."""

    def __init__(
        self, base_url: str = _DEFAULT_URL, model: str = _DEFAULT_MODEL, api_key: str = ""
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model or _DEFAULT_MODEL
        self.api_key = api_key

    def _headers(self) -> dict[str, str]:
        if self.api_key:
            return {"Authorization": f"Bearer {self.api_key}"}
        return {}

    def _record_usage(
        self,
        task: str,
        resp_json: dict,
        prompt_text: str,
        reply_text: str,
        extra_input_tokens: int = 0,
    ) -> None:
        """Record this call's token usage, preferring the API's own count.

        lemonade-server's OpenAI-compatible endpoint reports a real
        ``usage`` object when the underlying backend supports it; when it's
        missing (or incomplete) we fall back to a rough character-based
        estimate so local-model usage is never silently unreported.
        """
        usage = resp_json.get("usage") or {}
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")
        estimated = input_tokens is None or output_tokens is None
        if input_tokens is None:
            input_tokens = estimate_tokens(prompt_text) + extra_input_tokens
        if output_tokens is None:
            output_tokens = estimate_tokens(reply_text)
        record_model_usage(
            provider="lemonade",
            model=self.model,
            task=task,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated=estimated,
        )

    # ------------------------------------------------------------------
    # Availability check
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if lemonade-server is reachable."""
        try:
            with httpx.Client(timeout=5) as client:
                resp = client.get(f"{self.base_url}/v1/models", headers=self._headers())
                return resp.status_code == 200
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Text chat
    # ------------------------------------------------------------------

    def chat(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.2,
    ) -> str | None:
        """Send a text-only chat request; return the assistant reply or None."""
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                resp = client.post(
                    f"{self.base_url}/v1/chat/completions",
                    json=payload,
                    headers=self._headers(),
                )
            if resp.status_code != 200:
                logger.warning(
                    "lemonade chat error %s: %s", resp.status_code, resp.text[:200]
                )
                return None
            resp_json = resp.json()
            reply = resp_json["choices"][0]["message"]["content"]
            self._record_usage("chat", resp_json, prompt_text=f"{system}\n{prompt}", reply_text=reply)
            return reply
        except Exception as exc:
            logger.warning("lemonade chat failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Vision: compare two images
    # ------------------------------------------------------------------

    def vision_compare(
        self,
        baseline_bytes: bytes,
        new_bytes: bytes,
        snap_name: str,
        old_version: str,
        new_version: str,
    ) -> dict | None:
        """Compare two screenshots and return a structured decision dict.

        Returns a dict with keys ``decision`` (approve|reject|needs_review),
        ``confidence`` (0.0–1.0), and ``reasoning`` (str), or None on failure.
        """
        b64_baseline = base64.b64encode(baseline_bytes).decode()
        b64_new = base64.b64encode(new_bytes).decode()

        prompt = (
            f"You are reviewing a snap package update for '{snap_name}', "
            f"updating from version {old_version} to {new_version}.\n\n"
            "The first image is a screenshot from the PREVIOUS (baseline) version. "
            "The second image is a screenshot from the NEW version after testing.\n\n"
            "Evaluate whether the application appears to function correctly in the new version. "
            "Look for: visual regressions, crash dialogs, error messages, missing UI elements, "
            "or any sign the application is not working properly.\n\n"
            "Respond with ONLY a JSON object in this exact format:\n"
            '{"decision": "approve|reject|needs_review", '
            '"confidence": 0.0, '
            '"reasoning": "one paragraph explanation"}'
        )

        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64_baseline}"},
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64_new}"},
                    },
                ],
            }
        ]

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
        }
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                resp = client.post(
                    f"{self.base_url}/v1/chat/completions",
                    json=payload,
                    headers=self._headers(),
                )
            if resp.status_code != 200:
                logger.warning(
                    "lemonade vision error %s: %s", resp.status_code, resp.text[:200]
                )
                return None
            resp_json = resp.json()
            content = resp_json["choices"][0]["message"]["content"]
            self._record_usage(
                "vision_compare",
                resp_json,
                prompt_text=prompt,
                reply_text=content,
                extra_input_tokens=2 * ESTIMATED_TOKENS_PER_IMAGE,
            )
            return _parse_vision_response(content)
        except Exception as exc:
            logger.warning("lemonade vision_compare failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # PR description generator
    # ------------------------------------------------------------------

    def generate_pr_description(
        self,
        snap_name: str,
        part_name: str,
        old_version: str,
        new_version: str,
        release_notes: str,
    ) -> dict | None:
        """Generate a commit message and PR body for a version bump.

        Returns ``{"title": str, "body": str}`` or None.
        """
        prompt = (
            f"Generate a GitHub pull request for a snap package version bump.\n\n"
            f"Snap: {snap_name}\n"
            f"Part: {part_name}\n"
            f"Old version: {old_version}\n"
            f"New version: {new_version}\n"
            f"Upstream release notes:\n{release_notes[:2000]}\n\n"
            "Respond with ONLY a JSON object:\n"
            '{"title": "chore: update <part> to <version>", '
            '"body": "markdown PR description with changelog highlights"}'
        )
        result = self.chat(prompt, temperature=0.3)
        if result is None:
            return None
        try:
            start = result.find("{")
            end = result.rfind("}") + 1
            return json.loads(result[start:end])
        except Exception:
            return None


def _parse_vision_response(content: str) -> dict | None:
    """Extract structured decision from LLM response text."""
    try:
        start = content.find("{")
        end = content.rfind("}") + 1
        if start == -1 or end == 0:
            return None
        data = json.loads(content[start:end])
        decision = data.get("decision", "needs_review")
        if decision not in ("approve", "reject", "needs_review"):
            decision = "needs_review"
        confidence = float(data.get("confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))
        return {
            "decision": decision,
            "confidence": confidence,
            "reasoning": str(data.get("reasoning", "")),
        }
    except Exception as exc:
        logger.warning("failed to parse vision response: %s", exc)
        return None


def get_lemonade_client(
    user_config, ensure_started: bool = False, task: str = "text"
) -> LemonadeClient | None:
    """Return a LemonadeClient for this user, configured for ``task``.

    Backend selection is explicit via ``UserConfig.lemonade_backend``
    ("embedded" or "system"), defaulting to "embedded" — snap-dashboard's
    private, bundled "Embedded Lemonade" instance (see
    ``snap_dashboard.lemonade.embedded``), which is downloaded, run, and
    authenticated to on its own with no host lemonade-server dependency.

    When ``lemonade_backend`` is "system", the user's own self-managed
    lemonade-server is used instead — ``lemonade_server_url`` (required)
    and ``lemonade_api_key`` (optional, only needed if that server enforces
    one).

    ``task`` selects which opinionated default model to use when the user
    hasn't set an explicit ``lemonade_model`` override — see
    ``lemonade.models.TASK_MODELS`` (e.g. "vision" for screenshot review,
    "text" for PR-description generation, "coding" for the local coding
    backend).

    When ``ensure_started`` is True (safe from background agent threads,
    NOT from request-handling code paths) this will block briefly to start
    the embedded server on first use — on a cold machine this can take a
    while (binary download) — and kick off a background pull+load of the
    selected model (sized with its opinionated context window, see
    ``lemonade.models.TASK_CONTEXT_SIZES``) if it hasn't been fetched yet.
    """
    from snap_dashboard.lemonade.models import default_context_for, default_model_for

    backend = (getattr(user_config, "lemonade_backend", "") or "embedded").strip().lower()
    model_override = getattr(user_config, "lemonade_model", "") or ""
    task_model = model_override or default_model_for(task)

    if backend == "system":
        url = getattr(user_config, "lemonade_server_url", "") or ""
        if url:
            api_key = getattr(user_config, "lemonade_api_key", "") or ""
            return LemonadeClient(base_url=url, model=task_model, api_key=api_key)
        # "system" selected but no URL configured yet — nothing to talk to.
        logger.warning(
            "lemonade_backend=system but no lemonade_server_url configured; "
            "falling back to Embedded Lemonade."
        )

    from snap_dashboard.lemonade.embedded import get_embedded_manager

    manager = get_embedded_manager()
    if ensure_started:
        manager.ensure_started()
        manager.ensure_model_pulled(task_model, ctx_size=default_context_for(task))
    return LemonadeClient(
        base_url=manager.base_url,
        model=task_model,
        api_key=manager.api_key,
    )
