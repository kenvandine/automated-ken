"""Tests for the pluggable coding-task backend selector.

See agents/coding_backend.py: this is the single place that decides which
backend handles "capable coding" work (CI fixes, dep upgrades, issue fixes,
fleet normalization) — GitHub Copilot cloud agent today, with reserved
extension points for a future local model and an external API key.
"""

from __future__ import annotations

from types import SimpleNamespace

from snap_dashboard.agents.coding_backend import _CopilotWithLocalFallback, get_coding_dispatcher
from snap_dashboard.github.copilot_agent import CopilotAgentClient


def _uc(**kwargs) -> SimpleNamespace:
    defaults = dict(
        coding_task_backend="copilot_cloud_agent",
        bot_github_token="",
        github_token="",
        external_coding_api_key="",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_none_config_returns_none() -> None:
    assert get_coding_dispatcher(None) is None


def test_default_backend_is_copilot_cloud_agent() -> None:
    client = get_coding_dispatcher(_uc(bot_github_token="tok123"))
    assert isinstance(client, _CopilotWithLocalFallback)
    assert isinstance(client._copilot, CopilotAgentClient)
    assert client.token == "tok123"


def test_copilot_backend_falls_back_to_plain_github_token() -> None:
    client = get_coding_dispatcher(_uc(bot_github_token="", github_token="tok456"))
    assert isinstance(client, _CopilotWithLocalFallback)
    assert client.token == "tok456"


def test_copilot_backend_without_any_token_returns_none() -> None:
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


def test_external_api_backend_is_a_reserved_noop() -> None:
    assert get_coding_dispatcher(
        _uc(coding_task_backend="external_api", external_coding_api_key="sk-abc")
    ) is None


def test_external_api_backend_without_key_returns_none() -> None:
    assert get_coding_dispatcher(
        _uc(coding_task_backend="external_api", external_coding_api_key="")
    ) is None


def test_unknown_backend_returns_none() -> None:
    assert get_coding_dispatcher(_uc(coding_task_backend="something_bogus")) is None
