"""Single source of truth for how each agent type is presented in the UI.

Every ``BaseAgent`` subclass has a machine name (``agent_type``). The web UI
used to hard-code display names, descriptions, colours, and groupings for a
handful of them in several templates/JS maps, so newer agents showed up as
raw ``snake_case`` with no colour and never appeared on the Agents page at
all. Everything that needs to describe an agent — the Agents status grid,
agent badges, run-log filters, and the docs — reads from here instead.

``tests/test_agent_registry.py`` fails if an agent class is added without an
entry here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

GROUPS: list[tuple[str, str, str]] = [
    ("discovery", "Discovery", "Find out what changed in the Store, upstream, and your repos."),
    ("changes", "Changes", "Open pull requests and delegate coding work."),
    ("test_release", "Test & release", "Test candidates on runners, review results, and promote."),
    ("maintenance", "Maintenance", "Keep builds fresh and credentials in sync."),
]


@dataclass(frozen=True)
class AgentInfo:
    agent_type: str
    name: str
    description: str
    group: str
    schedule: str
    palette: int
    uses_ai: bool = False


_AGENTS: list[AgentInfo] = [
    # Discovery
    AgentInfo("collector", "Collector", "Refreshes channel maps, issues, and PRs from the Store and GitHub.",
              "discovery", "Collection interval (Settings)", 1),
    AgentInfo("release_scanner", "Release scanner", "Checks packaging repos for new upstream versions.",
              "discovery", "Release scan interval (Settings)", 1),
    AgentInfo("issue_pr_reviewer", "Issue & PR reviewer", "Summarizes open issues and PRs and flags what needs attention.",
              "discovery", "On demand", 1, uses_ai=True),
    # Changes
    AgentInfo("version_bumper", "Version bumper", "Opens version-bump PRs from the bot account.",
              "changes", "On demand", 2),
    AgentInfo("upstream_maintainer", "Upstream maintainer", "Delegates dependency upgrades and issue fixes on repos you own.",
              "changes", "Scheduled when enabled", 3),
    AgentInfo("repo_normalizer", "Repo normalizer", "Brings packaging repos in line with the canonical workflow and AGENTS.md.",
              "changes", "On demand", 3),
    AgentInfo("stack_updater", "Stack updater", "Checks a repo's framework and dependency stack for updates.",
              "changes", "On demand", 3),
    AgentInfo("build_failure_watcher", "Build failure watcher", "Spots failing packaging builds and dispatches a fix.",
              "changes", "Every 15 min", 3),
    AgentInfo("custom_prompt", "Custom task", "Runs an instruction you typed against a packaging repo.",
              "changes", "On demand", 3),
    AgentInfo("review_item_address", "Review follow-up", "Assigns a coding agent to an issue or PR from a review.",
              "changes", "On demand", 3),
    # Test & release
    AgentInfo("pr_monitor", "PR monitor", "Tracks CI on bump PRs and queues smoke tests.",
              "test_release", "Every 5 min", 4),
    AgentInfo("screenshot_reviewer", "Screenshot reviewer", "Compares runner screenshots with the stable baseline using a vision model.",
              "test_release", "On demand", 5, uses_ai=True),
    AgentInfo("test_run_auto_promoter", "Auto-promoter", "Reviews finished runs and promotes approved release sets.",
              "test_release", "On demand", 5, uses_ai=True),
    AgentInfo("test_failure_analyzer", "Failure analyzer", "Explains why a test run failed in plain English.",
              "test_release", "On demand", 6, uses_ai=True),
    AgentInfo("runner_watchdog", "Runner watchdog", "Frees runners stuck on stalled jobs.",
              "test_release", "Every 2 min", 4),
    # Maintenance
    AgentInfo("stale_build_scanner", "Stale build scanner", "Rebuilds snaps that haven't published within the staleness window.",
              "maintenance", "Scheduled", 7),
    AgentInfo("rebuild_all_snaps", "Rebuild all", "Dispatches a rebuild for every snap with a build workflow.",
              "maintenance", "On demand", 7),
    AgentInfo("rebuild_one_snap", "Rebuild", "Dispatches a rebuild for a single snap.",
              "maintenance", "On demand", 7),
    AgentInfo("snapcraft_credential_sync", "Credential sync", "Pushes the Store credential to every packaging repo.",
              "maintenance", "On demand", 8),
    AgentInfo("copilot_retry", "Coding task retry", "Retries coding tasks that failed to start.",
              "maintenance", "On demand", 8),
]

AGENTS: dict[str, AgentInfo] = {a.agent_type: a for a in _AGENTS}


def get_agent(agent_type: str | None) -> AgentInfo:
    """Return display info for ``agent_type``, with a readable fallback for
    unknown/legacy types so old AgentRun rows still render sensibly."""
    if agent_type and agent_type in AGENTS:
        return AGENTS[agent_type]
    label = (agent_type or "unknown").replace("_", " ").capitalize()
    return AgentInfo(agent_type or "unknown", label, "", "maintenance", "On demand", 0)


def grouped() -> list[dict]:
    """Agents grouped for the status grid, in display order."""
    return [
        {
            "key": key,
            "label": label,
            "description": desc,
            "agents": [a for a in _AGENTS if a.group == key],
        }
        for key, label, desc in GROUPS
    ]


def as_json() -> dict[str, dict]:
    """Registry as plain dicts, embedded in pages for client-side rendering."""
    return {k: asdict(v) for k, v in AGENTS.items()}
