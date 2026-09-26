"""Tests for Lemonade backend selection and opinionated per-task defaults.

See lemonade/client.py (backend switch: embedded default vs. system
lemonade-server) and lemonade/models.py (opinionated per-task models/context
sizes tuned for AMD Strix Halo).
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from snap_dashboard.lemonade.client import get_lemonade_client
from snap_dashboard.lemonade.models import (
    TASK_CODING,
    TASK_CONTEXT_SIZES,
    TASK_MODELS,
    TASK_TEXT,
    TASK_VISION,
    default_context_for,
    default_model_for,
)


def _uc(**kwargs) -> SimpleNamespace:
    defaults = dict(
        lemonade_backend="embedded",
        lemonade_server_url="",
        lemonade_api_key="",
        lemonade_model="",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class OpinionatedDefaultsTests(unittest.TestCase):
    def test_every_task_has_a_model_and_context_size(self) -> None:
        for task in (TASK_VISION, TASK_TEXT, TASK_CODING):
            self.assertIn(task, TASK_MODELS)
            self.assertIn(task, TASK_CONTEXT_SIZES)
            self.assertTrue(TASK_MODELS[task])
            self.assertGreater(TASK_CONTEXT_SIZES[task], 0)

    def test_default_model_for_unknown_task_falls_back_to_text(self) -> None:
        self.assertEqual(default_model_for("bogus"), TASK_MODELS[TASK_TEXT])

    def test_default_context_for_unknown_task_falls_back_to_text(self) -> None:
        self.assertEqual(default_context_for("bogus"), TASK_CONTEXT_SIZES[TASK_TEXT])


class BackendSelectionTests(unittest.TestCase):
    def test_defaults_to_embedded_backend(self) -> None:
        with patch("snap_dashboard.lemonade.embedded.get_embedded_manager") as get_mgr:
            manager = SimpleNamespace(base_url="http://127.0.0.1:13411", api_key="secret")
            get_mgr.return_value = manager
            client = get_lemonade_client(_uc(), task=TASK_VISION)
        self.assertEqual(client.base_url, "http://127.0.0.1:13411")
        self.assertEqual(client.model, TASK_MODELS[TASK_VISION])
        self.assertEqual(client.api_key, "secret")

    def test_missing_backend_field_still_defaults_to_embedded(self) -> None:
        uc = SimpleNamespace(lemonade_server_url="http://example.com:8000")
        with patch("snap_dashboard.lemonade.embedded.get_embedded_manager") as get_mgr:
            manager = SimpleNamespace(base_url="http://127.0.0.1:13411", api_key="secret")
            get_mgr.return_value = manager
            client = get_lemonade_client(uc)
        # No explicit lemonade_backend attribute -> embedded, URL ignored.
        self.assertEqual(client.base_url, "http://127.0.0.1:13411")

    def test_system_backend_uses_configured_url_and_key(self) -> None:
        uc = _uc(
            lemonade_backend="system",
            lemonade_server_url="http://gpu-box:8000",
            lemonade_api_key="tok-abc",
        )
        client = get_lemonade_client(uc, task=TASK_CODING)
        self.assertEqual(client.base_url, "http://gpu-box:8000")
        self.assertEqual(client.model, TASK_MODELS[TASK_CODING])
        self.assertEqual(client.api_key, "tok-abc")

    def test_system_backend_without_url_falls_back_to_embedded(self) -> None:
        with patch("snap_dashboard.lemonade.embedded.get_embedded_manager") as get_mgr:
            manager = SimpleNamespace(base_url="http://127.0.0.1:13411", api_key="secret")
            get_mgr.return_value = manager
            client = get_lemonade_client(_uc(lemonade_backend="system", lemonade_server_url=""))
        self.assertEqual(client.base_url, "http://127.0.0.1:13411")

    def test_model_override_applies_to_either_backend(self) -> None:
        uc = _uc(
            lemonade_backend="system",
            lemonade_server_url="http://gpu-box:8000",
            lemonade_model="my.custom.model",
        )
        client = get_lemonade_client(uc, task=TASK_TEXT)
        self.assertEqual(client.model, "my.custom.model")

    def test_ensure_started_pulls_task_model_with_its_context_size(self) -> None:
        with patch("snap_dashboard.lemonade.embedded.get_embedded_manager") as get_mgr:
            manager = SimpleNamespace(
                base_url="http://127.0.0.1:13411",
                api_key="secret",
                ensure_started=lambda: True,
                ensure_model_pulled=lambda model, ctx_size=None: None,
            )
            get_mgr.return_value = manager
            with patch.object(manager, "ensure_model_pulled") as ensure_pulled:
                get_lemonade_client(_uc(), ensure_started=True, task=TASK_CODING)
        ensure_pulled.assert_called_once_with(
            TASK_MODELS[TASK_CODING], ctx_size=TASK_CONTEXT_SIZES[TASK_CODING]
        )

    def test_ensure_started_wires_up_self_heal_callbacks(self) -> None:
        # Background-agent-safe path (ensure_started=True): the returned
        # client should be able to ask the manager to reload the model or
        # restart lemond itself if requests keep failing.
        with patch("snap_dashboard.lemonade.embedded.get_embedded_manager") as get_mgr:
            manager = SimpleNamespace(
                base_url="http://127.0.0.1:13411",
                api_key="secret",
                ensure_started=lambda: True,
                ensure_model_pulled=lambda model, ctx_size=None: None,
                reload_model=lambda model, ctx_size=None: True,
                restart=lambda reason="": True,
            )
            get_mgr.return_value = manager
            with (
                patch.object(manager, "reload_model", return_value=True) as reload_model,
                patch.object(manager, "restart", return_value=True) as restart,
            ):
                client = get_lemonade_client(_uc(), ensure_started=True, task=TASK_CODING)
                names = [name for name, _fn in client.heal_callbacks]
                self.assertEqual(names, ["reload_model", "restart_lemond"])
                # Each callback actually delegates to the manager's own method.
                for _name, fn in client.heal_callbacks:
                    fn()
            reload_model.assert_called_once_with(TASK_MODELS[TASK_CODING], ctx_size=TASK_CONTEXT_SIZES[TASK_CODING])
            restart.assert_called_once()

    def test_request_handling_path_has_no_self_heal_callbacks(self) -> None:
        # ensure_started=False (the request-handling-safe default) must not
        # wire up heal callbacks, since restart() can block for a while --
        # unsafe on a request thread.
        with patch("snap_dashboard.lemonade.embedded.get_embedded_manager") as get_mgr:
            manager = SimpleNamespace(base_url="http://127.0.0.1:13411", api_key="secret")
            get_mgr.return_value = manager
            client = get_lemonade_client(_uc(), ensure_started=False, task=TASK_CODING)
        self.assertEqual(client.heal_callbacks, [])

    def test_system_backend_has_no_self_heal_callbacks(self) -> None:
        # We don't own a self-managed lemonade-server's lifecycle.
        uc = _uc(
            lemonade_backend="system",
            lemonade_server_url="http://gpu-box:8000",
        )
        client = get_lemonade_client(uc, ensure_started=True, task=TASK_CODING)
        self.assertEqual(client.heal_callbacks, [])


if __name__ == "__main__":
    unittest.main()
