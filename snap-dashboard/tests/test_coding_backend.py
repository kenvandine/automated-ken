"""Tests for the pluggable coding-task backend selector.

See agents/coding_backend.py: this is the single place that decides which
backend handles "capable coding" work (CI fixes, dep upgrades, issue fixes,
fleet normalization) — a local model or an external OpenAI-compatible API.
"""

from __future__ import annotations

from types import SimpleNamespace

from snap_dashboard.agents.coding_backend import get_coding_dispatcher


def _uc(**kwargs) -> SimpleNamespace:
    defaults = dict(
        coding_task_backend="local_lemonade",
        bot_github_token="",
        github_token="",
        external_coding_api_key="",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_none_config_returns_none() -> None:
    assert get_coding_dispatcher(None) is None


def test_default_backend_is_local_lemonade() -> None:
    from snap_dashboard.lemonade.coding_agent import LocalLemonadeCodingDispatcher

    client = get_coding_dispatcher(_uc(bot_github_token="tok123"))
    assert isinstance(client, LocalLemonadeCodingDispatcher)
    assert client.token == "tok123"


def test_legacy_copilot_setting_falls_back_to_default() -> None:
    from snap_dashboard.lemonade.coding_agent import LocalLemonadeCodingDispatcher

    client = get_coding_dispatcher(_uc(coding_task_backend="copilot_cloud_agent", bot_github_token="t"))
    assert isinstance(client, LocalLemonadeCodingDispatcher)


def test_default_backend_falls_back_to_plain_github_token() -> None:
    client = get_coding_dispatcher(_uc(bot_github_token="", github_token="tok456"))
    assert client.token == "tok456"


def test_default_backend_without_any_token_returns_none() -> None:
    assert get_coding_dispatcher(_uc(bot_github_token="", github_token="")) is None


def test_local_lemonade_backend_without_token_returns_none() -> None:
    assert get_coding_dispatcher(
        _uc(coding_task_backend="local_lemonade", bot_github_token="", github_token="")
    ) is None


def test_local_lemonade_backend_returns_dispatcher_with_token() -> None:
    from snap_dashboard.lemonade.coding_agent import LocalLemonadeCodingDispatcher

    client = get_coding_dispatcher(_uc(coding_task_backend="local_lemonade", bot_github_token="tok789"))
    assert isinstance(client, LocalLemonadeCodingDispatcher)
    assert client.token == "tok789"


def test_external_api_backend_returns_dispatcher_when_configured() -> None:
    from snap_dashboard.lemonade.external_api import ExternalApiCodingDispatcher

    d = get_coding_dispatcher(_uc(
        coding_task_backend="external_api", bot_github_token="tok",
        external_coding_api_key="sk-abc",
        external_coding_api_base_url="https://example.test/v1",
        external_coding_api_model="m",
    ))
    assert isinstance(d, ExternalApiCodingDispatcher)


def test_external_api_backend_missing_url_or_model_returns_none() -> None:
    assert get_coding_dispatcher(
        _uc(coding_task_backend="external_api", bot_github_token="tok", external_coding_api_key="sk-abc")
    ) is None


def test_openai_compat_client_records_external_usage(monkeypatch) -> None:
    import httpx

    from snap_dashboard.lemonade import external_api

    calls = {}

    class Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": "hi"}}],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 3}}

    def fake_post(url, json, headers, timeout):
        calls.update(url=url, auth=headers["Authorization"])
        return Resp()

    recorded = []
    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(external_api, "record_model_usage", lambda **kw: recorded.append(kw))
    c = external_api.OpenAICompatClient("https://example.test/v1/", "sk", "m")
    assert c.chat("p", system="s", json_mode=True) == "hi"
    assert calls == {"url": "https://example.test/v1/chat/completions", "auth": "Bearer sk"}
    assert recorded[0]["provider"] == "external"
    assert (recorded[0]["input_tokens"], recorded[0]["output_tokens"], recorded[0]["estimated"]) == (7, 3, False)


def test_external_api_backend_without_key_returns_none() -> None:
    assert get_coding_dispatcher(
        _uc(coding_task_backend="external_api", bot_github_token="tok", external_coding_api_key="")
    ) is None


def test_unknown_backend_returns_none() -> None:
    assert get_coding_dispatcher(_uc(coding_task_backend="something_bogus")) is None


def test_list_models_queries_models_endpoint(monkeypatch) -> None:
    import httpx

    from snap_dashboard.lemonade import external_api

    seen = {}

    class Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"data": [{"id": "b"}, {"id": "a"}, {"id": "a"}, {}]}

    def fake_get(url, headers, timeout):
        seen["url"] = url
        return Resp()

    monkeypatch.setattr(httpx, "get", fake_get)
    assert external_api.list_models("https://example.test/v1/chat/completions", "sk") == ["a", "b"]
    assert seen["url"] == "https://example.test/v1/models"
