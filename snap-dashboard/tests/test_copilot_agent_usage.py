"""Tests for CopilotAgentClient's best-effort cloud token-usage recording.

The cloud-agent tasks API never reports real token usage, so start_task()
records a rough input-only estimate via snap_dashboard.telemetry — see
github/copilot_agent.py.
"""

from __future__ import annotations

from unittest.mock import patch

from snap_dashboard.github.copilot_agent import CopilotAgentClient


class _FakeResp:
    def __init__(self, json_data: dict, status_code: int = 201) -> None:
        self._json = json_data
        self.status_code = status_code
        self.text = "error"

    def json(self) -> dict:
        return self._json


class _FakeClient:
    def __init__(self, resp: _FakeResp) -> None:
        self._resp = resp

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json, headers):
        return self._resp


def test_start_task_records_estimated_input_only_usage():
    resp = _FakeResp({"id": "task-1", "status": "queued"})
    with patch("httpx.Client", return_value=_FakeClient(resp)), \
            patch("snap_dashboard.github.copilot_agent.record_model_usage") as record:
        result = CopilotAgentClient("tok").start_task(
            "kenvandine", "geforcenow", "b" * 40, model="gpt-5"
        )
    assert result == {"id": "task-1", "status": "queued"}
    record.assert_called_once_with(
        provider="copilot",
        model="gpt-5",
        task="coding_agent",
        input_tokens=10,  # 40 chars / 4
        output_tokens=0,
        estimated=True,
    )


def test_start_task_defaults_model_label_when_unspecified():
    resp = _FakeResp({"id": "task-2"})
    with patch("httpx.Client", return_value=_FakeClient(resp)), \
            patch("snap_dashboard.github.copilot_agent.record_model_usage") as record:
        CopilotAgentClient("tok").start_task("kenvandine", "geforcenow", "hi")
    assert record.call_args.kwargs["model"] == "copilot-default"


def test_failed_dispatch_does_not_record_usage():
    resp = _FakeResp({}, status_code=500)
    with patch("httpx.Client", return_value=_FakeClient(resp)), \
            patch("snap_dashboard.github.copilot_agent.record_model_usage") as record:
        result = CopilotAgentClient("tok").start_task("kenvandine", "geforcenow", "hi")
    assert result is None
    record.assert_not_called()
