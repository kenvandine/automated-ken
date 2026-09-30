"""Every agent class must have a UI registry entry (agents/registry.py), so
new agents show up on the Agents page with a real name and colour instead of
a raw snake_case type."""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import snap_dashboard.agents as agents_pkg
from snap_dashboard.agents import registry
from snap_dashboard.agents.base import BaseAgent


def _all_agent_types() -> set[str]:
    types: set[str] = set()
    for mod_info in pkgutil.iter_modules(agents_pkg.__path__):
        mod = importlib.import_module(f"snap_dashboard.agents.{mod_info.name}")
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            if issubclass(obj, BaseAgent) and obj is not BaseAgent:
                t = getattr(obj, "agent_type", None)
                if t and t != BaseAgent.agent_type:
                    types.add(t)
    return types


def test_every_agent_type_is_registered():
    missing = _all_agent_types() - set(registry.AGENTS)
    assert not missing, f"add these to agents/registry.py: {sorted(missing)}"


def test_registry_groups_are_valid():
    group_keys = {g[0] for g in registry.GROUPS}
    for info in registry.AGENTS.values():
        assert info.group in group_keys
        assert info.name and info.description


def test_unknown_agent_type_falls_back():
    info = registry.get_agent("some_legacy_type")
    assert info.name == "Some legacy type"
    assert info.palette == 0
