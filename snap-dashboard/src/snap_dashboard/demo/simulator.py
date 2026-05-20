"""Demo mode simulator — runs as a background thread pool inside the FastAPI app.

Started automatically when SNAP_DASHBOARD_DEMO=1 is set.
Drives two concurrent behaviours:

1. PipelineAdvancer  — advances the lemonade-desktop version bump PR through every
   pipeline stage on a fixed schedule so the Playwright recording can show the
   full end-to-end flow happening live.

2. ActivityFountain  — continuously fires short-lived agent activity messages
   into the in-memory ActivityTracker so the /agents feed looks furiously busy
   throughout the recording.
"""

from __future__ import annotations

import base64
import logging
import random
import threading
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Timing (seconds from simulator start)
# ---------------------------------------------------------------------------
# Keep these in sync with the Playwright script's navigation timing.
PIPELINE_SCHEDULE: list[tuple[int, str, str]] = [
    # (t_seconds, new_status, activity_message)
    (18,  "ci_pending",   "PR Monitor: CI build queued for lemonade-desktop PR #41"),
    (40,  "ci_passed",    "PR Monitor: CI ✓ passed for lemonade-desktop v10.4.0 — triggering YARF"),
    (65,  "yarf_running", "YARF: snap tests started for lemonade-desktop v10.4.0 on amd64"),
    (125, "yarf_passed",  "YARF: all tests passed for lemonade-desktop v10.4.0 ✓"),
    (132, None,           "⚡ Lemonade AI: loading vision model for screenshot comparison…"),
    (140, None,           "⚡ Lemonade AI: comparing lemonade-desktop v10.3.0 baseline → v10.4.0"),
    (162, "agent_approved",
          "Screenshot Reviewer: lemonade-desktop approved (confidence=93%) — UI consistent, no regressions"),
    (175, "promoting",    "Stable Promoter: promoting lemonade-desktop v10.4.0 to stable…"),
    (192, "stable_promoted",
          "lemonade-desktop v10.4.0 promoted to stable ✓"),
]

# Random background chatter fired every CHATTER_INTERVAL seconds
CHATTER_INTERVAL = 7
CHATTER: list[tuple[str, str | None, str]] = [
    # (agent_type, snap_name_or_None, message)
    ("release_scanner",    "ghostty",          "Checking ghostty for upstream updates"),
    ("release_scanner",    "warble",           "Checking warble for upstream updates"),
    ("release_scanner",    "terminal-fun",     "Checking terminal-fun for upstream updates"),
    ("release_scanner",    "gemini-desktop",   "Scanning gemini-desktop packaging repo"),
    ("pr_monitor",         "warble",           "Polling CI status: warble PR #87"),
    ("pr_monitor",         "gemini-desktop",   "Polling CI status: gemini-desktop PR #23"),
    ("pr_monitor",         "ghostty",          "Checking YARF result for ghostty PR #5201"),
    ("stale_build_scanner","terminal-2048",    "terminal-2048: candidate build still in progress"),
    ("stale_build_scanner","terminal-solitaire","terminal-solitaire: rebuild dispatched, awaiting CI"),
    ("stale_build_scanner","terminal-tetris",  "terminal-tetris: candidate build completed ✓"),
    ("version_bumper",     "warble",           "Version Bumper: monitoring warble PR #87 for mergeability"),
    ("release_scanner",    None,               "Release scanner: checking 80 snaps for upstream changes"),
    ("pr_monitor",         "lemonade-desktop", "PR Monitor: watching lemonade-desktop CI pipeline"),
]

# How long each chatter message stays "active" before clearing
CHATTER_DURATION = 3


class DemoSimulator:
    """Starts both simulation threads and exposes a stop() method."""

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        logger.info("Demo simulator starting.")
        for target in (self._pipeline_advancer, self._activity_fountain):
            t = threading.Thread(target=target, daemon=True, name=target.__name__)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop_event.set()

    # ------------------------------------------------------------------

    def _pipeline_advancer(self) -> None:
        """Advance the lemonade-desktop PR through the pipeline on schedule."""
        from snap_dashboard.db.models import AgentRun, ScreenshotComparison, VersionBumpPR
        from snap_dashboard.db.session import get_session
        from snap_dashboard.agents.runner import get_tracker

        # Find the lemonade-desktop PR ID (seeded by seed_demo.py)
        bump_id: int | None = None
        deadline = time.monotonic() + 30
        while bump_id is None and time.monotonic() < deadline:
            try:
                with get_session() as s:
                    bump = (
                        s.query(VersionBumpPR)
                        .join(VersionBumpPR.snap)
                        .filter_by(name="lemonade-desktop")
                        .first()
                    )
                    if bump:
                        bump_id = bump.id
            except Exception:
                pass
            if bump_id is None:
                time.sleep(2)

        if bump_id is None:
            logger.warning("Demo: lemonade-desktop PR not found — pipeline advancer exiting.")
            return

        start = time.monotonic()
        stage_idx = 0

        while not self._stop_event.is_set() and stage_idx < len(PIPELINE_SCHEDULE):
            t_target, new_status, message = PIPELINE_SCHEDULE[stage_idx]
            elapsed = time.monotonic() - start

            if elapsed >= t_target:
                # Fire activity message
                tracker = get_tracker()
                agent_type = _agent_type_for_message(message)
                tracker.set_active(agent_type, message, "lemonade-desktop")

                # Persist AgentRun record for history
                with get_session() as s:
                    run = AgentRun(
                        user_id=1,
                        agent_type=agent_type,
                        snap_name="lemonade-desktop",
                        status="running" if new_status is None else "done",
                        result_summary=message if new_status else None,
                        started_at=datetime.now(timezone.utc),
                        finished_at=datetime.now(timezone.utc) if new_status else None,
                    )
                    s.add(run)

                    if new_status:
                        bump = s.query(VersionBumpPR).get(bump_id)
                        if bump:
                            bump.status = new_status
                            bump.updated_at = datetime.now(timezone.utc)

                            if new_status == "agent_approved":
                                bump.agent_decision = "approve"
                                bump.agent_confidence = 0.93
                                bump.agent_reasoning = (
                                    "Both screenshots show the Lemonade Desktop chat interface "
                                    "in a consistent state. The v10.4.0 build renders identically "
                                    "to the v10.3.0 baseline — same layout, colour palette, and "
                                    "sidebar structure. No error dialogs, blank panels, or visual "
                                    "regressions detected. Confidence 93%."
                                )
                                # Create ScreenshotComparison record
                                sc = _build_screenshot_comparison(bump_id)
                                if sc:
                                    s.add(sc)

                time.sleep(CHATTER_DURATION)
                tracker.clear_active(agent_type)
                stage_idx += 1
            else:
                time.sleep(1)

        logger.info("Demo pipeline advancer finished.")

    def _activity_fountain(self) -> None:
        """Continuously emit random agent activity into the in-memory tracker."""
        from snap_dashboard.agents.runner import get_tracker

        tracker = get_tracker()
        chatter = list(CHATTER)
        random.shuffle(chatter)
        idx = 0

        while not self._stop_event.is_set():
            agent_type, snap_name, message = chatter[idx % len(chatter)]
            tracker.set_active(agent_type, message, snap_name or "")
            self._stop_event.wait(CHATTER_DURATION)
            tracker.clear_active(agent_type)
            idx += 1
            jitter = random.uniform(1.5, 4.0)
            self._stop_event.wait(jitter)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _agent_type_for_message(msg: str) -> str:
    msg_lower = msg.lower()
    if "lemonade ai" in msg_lower or "⚡" in msg_lower or "screenshot" in msg_lower:
        return "screenshot_reviewer"
    if "pr monitor" in msg_lower or "ci" in msg_lower or "yarf" in msg_lower and "yarf:" not in msg_lower:
        return "pr_monitor"
    if "yarf:" in msg_lower:
        return "pr_monitor"
    if "version bumper" in msg_lower or "opened pr" in msg_lower:
        return "version_bumper"
    if "stable promoter" in msg_lower or "promoting" in msg_lower or "promoted" in msg_lower:
        return "stable_promoter"
    return "release_scanner"


def _build_screenshot_comparison(bump_id: int):
    """Create a ScreenshotComparison row using the seeded baseline images."""
    try:
        import importlib.util, os
        from snap_dashboard.db.models import ScreenshotComparison, StableScreenshotBaseline
        from snap_dashboard.db.session import get_session

        # Load screenshot generator
        demo_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..", "demo")
        spec = importlib.util.spec_from_file_location(
            "screenshots", os.path.join(demo_dir, "screenshots.py")
        )
        sc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sc)

        before_bytes = sc.lemonade_desktop_before()
        after_bytes = sc.lemonade_desktop_after()

        return ScreenshotComparison(
            version_bump_pr_id=bump_id,
            baseline_image_b64=base64.b64encode(before_bytes).decode(),
            new_image_b64=base64.b64encode(after_bytes).decode(),
            llm_prompt="vision_compare",
            llm_response='{"decision":"approve","confidence":0.93,"reasoning":"UI consistent."}',
            decision="approve",
            confidence=0.93,
            reasoning=(
                "Both screenshots show the Lemonade Desktop chat interface in a consistent state. "
                "The v10.4.0 build renders identically to the v10.3.0 baseline — same layout, "
                "colour palette, and sidebar structure. No error dialogs, blank panels, or visual "
                "regressions detected. Confidence 93%."
            ),
            analyzed_at=datetime.now(timezone.utc),
        )
    except Exception as exc:
        logger.warning("Demo: could not build screenshot comparison: %s", exc)
        return None
