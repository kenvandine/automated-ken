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

logger = logging.getLogger(__name__)

_DEFAULT_URL = "http://localhost:8080"
_DEFAULT_MODEL = "llava"
_TIMEOUT = 120  # seconds — vision inference can be slow on CPU


class LemonadeClient:
    """Thin client for lemonade-server's OpenAI-compatible API."""

    def __init__(self, base_url: str = _DEFAULT_URL, model: str = _DEFAULT_MODEL) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model or _DEFAULT_MODEL

    # ------------------------------------------------------------------
    # Availability check
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if lemonade-server is reachable."""
        try:
            with httpx.Client(timeout=5) as client:
                resp = client.get(f"{self.base_url}/v1/models")
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
                )
            if resp.status_code != 200:
                logger.warning("lemonade chat error %s: %s", resp.status_code, resp.text[:200])
                return None
            return resp.json()["choices"][0]["message"]["content"]
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
                )
            if resp.status_code != 200:
                logger.warning(
                    "lemonade vision error %s: %s", resp.status_code, resp.text[:200]
                )
                return None
            content = resp.json()["choices"][0]["message"]["content"]
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


def get_lemonade_client(user_config) -> LemonadeClient | None:
    """Return a LemonadeClient if the user has a lemonade URL configured."""
    url = getattr(user_config, "lemonade_server_url", "") or ""
    model = getattr(user_config, "lemonade_model", "") or _DEFAULT_MODEL
    if not url:
        url = _DEFAULT_URL
    return LemonadeClient(base_url=url, model=model)
