"""Single shared Jinja2 template environment for the whole web app.

Every route module used to instantiate its own ``Jinja2Templates(...)``
pointed at the same templates directory — harmless for rendering, but it
meant registering a Jinja global (like ``app_version()``, used in
base.html's footer) on just one of those instances left it undefined in
every other router, since each had its own separate ``jinja2.Environment``.
Importing this single instance everywhere fixes that for good.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.templating import Jinja2Templates

from snap_dashboard.agents import registry as agent_registry
from snap_dashboard.version import get_app_version

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["app_version"] = get_app_version


# ---------------------------------------------------------------------------
# Shared UI helpers — see templates/_ui.html for the macros that use them.
# ---------------------------------------------------------------------------

# Every status string the app produces, mapped to one of five visual tones
# (positive / caution / negative / info / neutral) and a human label. Pages
# used to each pick their own colour for the same status, so "passed" or
# "needs_review" looked different on Testing, Version Bumps, and the Snap
# page. Unknown statuses fall back to neutral with a humanized label.
STATUS_TONES: dict[str, tuple[str, str]] = {
    # Test runs
    "pending": ("neutral", "Pending"),
    "triggered": ("info", "Queued"),
    "running": ("info", "Running"),
    "reviewing": ("info", "AI reviewing"),
    "passed": ("positive", "Passed"),
    "failed": ("negative", "Failed"),
    "error": ("negative", "Error"),
    "promoted": ("positive", "Promoted"),
    "cancelled": ("neutral", "Cancelled"),
    "ready": ("positive", "Ready"),
    # AI review decisions
    "approve": ("positive", "AI approved"),
    "reject": ("negative", "AI rejected"),
    "needs_review": ("caution", "Needs review"),
    # Version-bump PRs
    "open": ("info", "Open"),
    "ci_pending": ("info", "CI running"),
    "ci_passed": ("info", "CI passed"),
    "ci_failed": ("negative", "CI failed"),
    "yarf_running": ("info", "Testing"),
    "yarf_passed": ("info", "Tests passed"),
    "yarf_failed": ("negative", "Tests failed"),
    "agent_approved": ("caution", "Ready to merge"),
    "agent_rejected": ("negative", "Agent rejected"),
    "merged": ("positive", "Merged"),
    "awaiting_release": ("info", "Awaiting build"),
    "candidate_testing": ("info", "Candidate testing"),
    "stable_promoted": ("positive", "Promoted to stable"),
    "stable_promoted_partial": ("caution", "Partly promoted"),
    "closed": ("neutral", "Closed"),
    "dispatched": ("info", "Coding agent working"),
    # Agent runs / coding tasks
    "done": ("positive", "Done"),
    "queued": ("neutral", "Queued"),
    "in_progress": ("info", "In progress"),
    "waiting_for_user": ("caution", "Waiting for you"),
    "dispatch_failed": ("negative", "Dispatch failed"),
    "timed_out": ("negative", "Timed out"),
    "completed": ("positive", "Completed"),
    "success": ("positive", "Success"),
    "failure": ("negative", "Failed"),
    # Runners
    "online": ("positive", "Online"),
    "offline": ("neutral", "Offline"),
    "busy": ("info", "Busy"),
    "idle": ("positive", "Idle"),
    # Snap health
    "ok": ("positive", "OK"),
    "stale": ("caution", "Stale"),
    "unknown": ("neutral", "Unknown"),
}


def status_info(status: str | None) -> dict[str, str]:
    key = (status or "unknown").lower()
    tone, label = STATUS_TONES.get(key, ("neutral", key.replace("_", " ").capitalize()))
    return {"tone": tone, "label": label}


def format_dt(value, fmt: str = "short") -> str:
    """Consistent timestamp rendering. Naive datetimes are treated as UTC
    (that's how the DB stores them). ``fmt``: ``short`` → "Jan 03 14:05",
    ``long`` → "2025-01-03 14:05 UTC", ``relative`` → "3h ago"."""
    if not value:
        return "—"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    if fmt == "long":
        return value.strftime("%Y-%m-%d %H:%M UTC")
    if fmt == "relative":
        secs = (datetime.now(timezone.utc) - value).total_seconds()
        future = secs < 0
        secs = abs(secs)
        if secs > 30 * 86400:
            return value.strftime("%Y-%m-%d")
        if secs < 60:
            text = "just now" if not future else "in <1m"
            return text
        for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
            if secs >= size:
                n = int(secs // size)
                return f"in {n}{unit}" if future else f"{n}{unit} ago"
    return value.strftime("%b %d %H:%M")


def nav_active(request, prefixes) -> bool:
    """True if the current request path starts with any of ``prefixes``
    (``"/"`` only matches exactly). Defensive about ``request`` so templates
    rendered in tests with a minimal fake request still work."""
    url = getattr(request, "url", None)
    path = getattr(url, "path", None)
    if not isinstance(path, str):
        return False
    if isinstance(prefixes, str):
        prefixes = [prefixes]
    for p in prefixes:
        if p == "/":
            if path == "/":
                return True
        elif path == p or path.startswith(p.rstrip("/") + "/"):
            return True
    return False


templates.env.filters["dt"] = format_dt
templates.env.globals["status_info"] = status_info
templates.env.globals["status_tones_json"] = lambda: {k: list(v) for k, v in STATUS_TONES.items()}
templates.env.globals["nav_active"] = nav_active
templates.env.globals["agent_info"] = agent_registry.get_agent
templates.env.globals["agent_groups"] = agent_registry.grouped
templates.env.globals["agent_registry_json"] = agent_registry.as_json
