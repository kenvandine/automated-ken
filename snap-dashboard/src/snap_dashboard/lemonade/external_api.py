"""``external_api`` coding backend: any OpenAI-compatible chat-completions
endpoint (OpenAI, Azure/OpenRouter/vLLM/Ollama gateways, ...) with an API key.

Reuses the single-shot plan/commit/PR flow of ``LocalLemonadeCodingDispatcher``
and only swaps the model client. Token usage is recorded under provider
``external`` so it shows up as cloud usage on ``/stats``.
"""

from __future__ import annotations

import logging

import httpx

from snap_dashboard.lemonade.coding_agent import LocalLemonadeCodingDispatcher
from snap_dashboard.telemetry import estimate_tokens, record_model_usage

logger = logging.getLogger(__name__)

EXTERNAL_PROVIDER = "external"
_DEFAULT_TIMEOUT = 600.0


class OpenAICompatClient:
    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    @property
    def _url(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def chat(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.1,
        max_tokens: int | None = None,
        timeout: float | None = None,
        json_mode: bool = False,
        **_ignored,
    ) -> str | None:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: dict = {"model": self.model, "messages": messages, "temperature": temperature}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            resp = httpx.post(
                self._url,
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=timeout or _DEFAULT_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            reply = data["choices"][0]["message"]["content"]
        except Exception as exc:
            logger.warning("external coding API call failed (model=%s): %s", self.model, exc)
            return None
        if not reply:
            return None
        usage = data.get("usage") or {}
        in_tokens = usage.get("prompt_tokens")
        out_tokens = usage.get("completion_tokens")
        record_model_usage(
            provider=EXTERNAL_PROVIDER,
            model=self.model,
            task="coding_agent",
            input_tokens=estimate_tokens(f"{system}\n{prompt}") if in_tokens is None else in_tokens,
            output_tokens=estimate_tokens(reply) if out_tokens is None else out_tokens,
            estimated=in_tokens is None or out_tokens is None,
        )
        return reply


class ExternalApiCodingDispatcher(LocalLemonadeCodingDispatcher):
    def _get_client(self):
        uc = self._uc
        base_url = (getattr(uc, "external_coding_api_base_url", "") or "").strip()
        api_key = (getattr(uc, "external_coding_api_key", "") or "").strip()
        model = (getattr(uc, "external_coding_api_model", "") or "").strip()
        if not (base_url and api_key and model):
            return None, "External coding API needs base URL, API key and model configured"
        return OpenAICompatClient(base_url, api_key, model), None


def list_models(base_url: str, api_key: str, timeout: float = 15.0) -> list[str]:
    """Return model ids from the endpoint's ``GET /models`` (OpenAI-compatible).

    Raises on HTTP/parse errors so callers can surface the reason.
    """
    url = base_url.strip().rstrip("/")
    if url.endswith("/chat/completions"):
        url = url[: -len("/chat/completions")]
    resp = httpx.get(f"{url}/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout)
    resp.raise_for_status()
    items = resp.json().get("data") or []
    return sorted({m["id"] for m in items if isinstance(m, dict) and m.get("id")})
