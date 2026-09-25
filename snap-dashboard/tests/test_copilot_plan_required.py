"""Regression coverage for handling GitHub's ``copilot_plan_required`` 403.

A ``copilot_plan_required`` 403 means the whole account has no Copilot
license — an account-wide condition, not a per-repo one. Before this fix,
every repo/PR/issue in a batch (e.g. RepoNormalizerAgent looping over the
whole fleet) would independently retry and log the same 403, and the
default ``copilot_cloud_agent`` backend would just fail forever with no way
to make progress on an account with no license. See github/copilot_agent.py
and agents/coding_backend.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from snap_dashboard.agents.coding_backend import get_coding_dispatcher
from snap_dashboard.github.copilot_agent import CopilotAgentClient


class _FakeResp:
    def __init__(self, json_data: dict, status_code: int = 201) -> None:
        self._json = json_data
        self.status_code = status_code
        self.text = str(json_data)

    def json(self) -> dict:
        return self._json


class _FakeClient:
    """Records how many POSTs actually reach the network."""

    def __init__(self, resp: _FakeResp) -> None:
        self._resp = resp
        self.post_count = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json, headers):
        self.post_count += 1
        return self._resp


_PLAN_REQUIRED_RESP = _FakeResp(
    {"code": "copilot_plan_required", "error": "forbidden", "message": "This API requires a valid Copilot license."},
    status_code=403,
)


def test_start_task_marks_plan_required_on_403():
    client = CopilotAgentClient("tok")
    fake = _FakeClient(_PLAN_REQUIRED_RESP)
    with patch("httpx.Client", return_value=fake):
        result = client.start_task("kenvandine", "repo-a", "prompt")
    assert result is None
    assert client.plan_required is True
    assert fake.post_count == 1


def test_start_task_skips_network_after_plan_required_seen():
    client = CopilotAgentClient("tok")
    fake = _FakeClient(_PLAN_REQUIRED_RESP)
    with patch("httpx.Client", return_value=fake):
        client.start_task("kenvandine", "repo-a", "prompt")
        # Second call, second repo — should short-circuit without a
        # second network round-trip (and without a second noisy 403 log).
        result = client.start_task("kenvandine", "repo-b", "prompt")
    assert result is None
    assert fake.post_count == 1


def test_other_403s_do_not_set_plan_required():
    other_403 = _FakeResp({"code": "something_else"}, status_code=403)
    client = CopilotAgentClient("tok")
    with patch("httpx.Client", return_value=_FakeClient(other_403)):
        result = client.start_task("kenvandine", "repo-a", "prompt")
    assert result is None
    assert client.plan_required is False


def _uc(**kwargs) -> SimpleNamespace:
    defaults = dict(
        coding_task_backend="copilot_cloud_agent",
        bot_github_token="tok123",
        github_token="",
        external_coding_api_key="",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_default_backend_falls_back_to_local_lemonade_without_a_license():
    """The composite dispatcher returned for the default backend should
    delegate to the local Lemonade backend once Copilot reports no license,
    instead of every remaining dispatch in the run just failing."""
    dispatcher = get_coding_dispatcher(_uc())
    fake = _FakeClient(_PLAN_REQUIRED_RESP)

    fake_local_result = {"state": "completed", "html_url": "https://github.com/kenvandine/repo-a/pull/1"}

    with patch("httpx.Client", return_value=fake), \
            patch(
                "snap_dashboard.lemonade.coding_agent.LocalLemonadeCodingDispatcher.start_task",
                return_value=fake_local_result,
            ) as local_start:
        result = dispatcher.start_task("kenvandine", "repo-a", "prompt")

    assert result == fake_local_result
    local_start.assert_called_once()
    assert fake.post_count == 1
