#!/usr/bin/env python3
"""Seed the database with realistic demo data for the presentation recording.

Run ONCE before starting the demo server:
    cd snap-dashboard
    source .venv/bin/activate
    python demo/seed_demo.py

Re-running is safe — it clears previous demo data first.
"""

from __future__ import annotations

import base64
import os
import sys
from datetime import datetime, timedelta, timezone

# Make sure the package is importable when run from snap-dashboard/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from snap_dashboard.db.session import get_session, init_db
from snap_dashboard.db.models import (
    AgentRun,
    ScreenshotComparison,
    StableScreenshotBaseline,
    StaleBuildTrigger,
    TestRun,
    UpstreamRelease,
    VersionBumpPR,
)

# ---------------------------------------------------------------------------
# Snap IDs from the real database (verified 2026-05-20)
# ---------------------------------------------------------------------------
SNAP_IDS = {
    "ghostty":          2,
    "gemini-desktop":   3,
    "warble":           5,
    "lemonade-desktop": 80,
    "thrive":           34,
    "terminal-fun":     49,
    "terminal-2048":    35,
    "terminal-tetris":  14,
    "terminal-solitaire": 41,
}
USER_ID = 1


def _now(delta_seconds: int = 0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# ---------------------------------------------------------------------------
# Clear previous demo data
# ---------------------------------------------------------------------------

def _clear(session) -> None:
    print("Clearing previous demo data…")
    # Delete ScreenshotComparison rows linked to this user's VersionBumpPRs
    user_bump_ids = [
        r[0] for r in session.query(VersionBumpPR.id).filter_by(user_id=USER_ID)
    ]
    if user_bump_ids:
        session.query(ScreenshotComparison).filter(
            ScreenshotComparison.version_bump_pr_id.in_(user_bump_ids)
        ).delete(synchronize_session=False)

    # Delete UpstreamRelease rows linked to this user's snaps
    user_snap_ids = list(SNAP_IDS.values())
    session.query(UpstreamRelease).filter(
        UpstreamRelease.snap_id.in_(user_snap_ids)
    ).delete(synchronize_session=False)

    # Models with user_id
    for model in (StableScreenshotBaseline, StaleBuildTrigger, VersionBumpPR, TestRun, AgentRun):
        session.query(model).filter_by(user_id=USER_ID).delete()
    session.flush()


# ---------------------------------------------------------------------------
# Upstream releases
# ---------------------------------------------------------------------------

def _seed_upstream_releases(session) -> dict[str, UpstreamRelease]:
    print("Seeding upstream releases…")
    releases = {}

    specs = [
        ("ghostty",          2,  "ghostty",          "v1.3.1",                  "v1.3.2",   "git",   "https://github.com/ghostty-org/ghostty"),
        ("lemonade-desktop", 80, "lemonade-desktop",  "v10.3.0",                 "v10.4.0",  "git",   "https://github.com/lemonade-sdk/lemonade-desktop"),
        ("thrive",           34, "thrive",            "v0.9.2",                  "v1.0.1",   "git",   "https://github.com/Revolutionary-Games/Thrive"),
        ("warble",            5, "warble",            "v2.0.1",                  "v2.2.0",   "git",   "https://github.com/avojak/warble"),
        ("gemini-desktop",    3, "gemini-desktop",    "0.6.0-1-g69bbec4ce7",     "0.7.0",    "git",   "https://github.com/kenvandine/gemini-desktop"),
    ]

    for key, snap_id, part_name, current, latest, src_type, src_url in specs:
        rel = UpstreamRelease(
            snap_id=snap_id,
            part_name=part_name,
            source_type=src_type,
            source_url=src_url,
            current_version=current,
            latest_version=latest,
            release_url=f"{src_url}/releases/tag/{latest}",
            release_notes=f"Release notes for {latest}:\n- Bug fixes and performance improvements\n- Updated dependencies",
            discovered_at=_now(-3600),
            acted_on=key != "lemonade-desktop",
            acted_at=_now(-3400) if key != "lemonade-desktop" else None,
        )
        session.add(rel)
        releases[key] = rel

    session.flush()
    return releases


# ---------------------------------------------------------------------------
# Test runs (linked to existing PRs that went through YARF)
# ---------------------------------------------------------------------------

def _seed_test_runs(session) -> dict[str, TestRun]:
    print("Seeding test runs…")
    runs = {}

    specs = [
        ("ghostty",    "v1.3.2", 101, "passed"),
        ("thrive",     "v1.0.1", 102, "failed"),
        ("warble",     "v2.2.0", 103, "running"),
    ]

    for snap_name, version, pr_num, status in specs:
        tr = TestRun(
            user_id=USER_ID,
            snap_name=snap_name,
            architecture="amd64",
            from_channel="candidate",
            version=version,
            revision=None,
            status=status,
            pr_number=pr_num,
            pr_url=f"https://github.com/kenvandine/yarf-tests/pull/{pr_num}",
            triggered_by="auto",
            started_at=_now(-2800),
            finished_at=_now(-2400) if status != "running" else None,
            promoted=False,
        )
        session.add(tr)
        runs[snap_name] = tr

    session.flush()
    return runs


# ---------------------------------------------------------------------------
# Version bump PRs
# ---------------------------------------------------------------------------

def _seed_version_bump_prs(
    session,
    releases: dict,
    test_runs: dict,
    screenshots: dict[str, tuple[bytes, bytes]],
) -> dict[str, VersionBumpPR]:
    print("Seeding version bump PRs…")
    bumps = {}

    specs = [
        # (key, snap_id, old, new, repo, pr_num, status, decision, confidence, reasoning)
        (
            "ghostty", 2, "v1.3.1", "v1.3.2",
            "ghostty-org/ghostty", 5201,
            "agent_approved", "approve", 0.94,
            "Both screenshots show the same Ghostty terminal window with identical layout, "
            "colour scheme, and font rendering. The v1.3.2 build produces no visible "
            "regressions — the UI is pixel-consistent with the stable baseline. "
            "Benchmark output confirms improved startup latency. Safe to merge.",
        ),
        (
            "thrive", 34, "v0.9.2", "v1.0.1",
            "kenvandine/thrive-snap", 12,
            "agent_rejected", "reject", 0.91,
            "The new version screenshot shows a fatal crash dialog (SIGSEGV) immediately "
            "after launch. The baseline v0.9.2 screenshot shows the game running normally "
            "with no errors. This is a clear regression — the snap package likely has a "
            "missing library or broken wrapper script in the v1.0.1 build. Do not merge.",
        ),
        (
            "warble", 5, "v2.0.1", "v2.2.0",
            "avojak/warble", 87,
            "yarf_running", None, None, None,
        ),
        (
            "gemini-desktop", 3, "0.6.0-1-g69bbec4ce7", "0.7.0",
            "kenvandine/gemini-desktop", 23,
            "ci_pending", None, None, None,
        ),
        (
            "lemonade-desktop", 80, "v10.3.0", "v10.4.0",
            "lemonade-sdk/lemonade-desktop-snap", 41,
            "open", None, None, None,
        ),
    ]

    for key, snap_id, old_v, new_v, repo, pr_num, status, decision, conf, reasoning in specs:
        rel = releases.get(key)
        tr = test_runs.get(key)

        bump = VersionBumpPR(
            snap_id=snap_id,
            upstream_release_id=rel.id if rel else None,
            user_id=USER_ID,
            bot_pr_url=f"https://github.com/{repo}/pull/{pr_num}",
            bot_pr_number=pr_num,
            packaging_repo=repo,
            branch_name=f"version-bump/{key}/{new_v}",
            old_version=old_v,
            new_version=new_v,
            status=status,
            test_run_id=tr.id if tr else None,
            agent_decision=decision,
            agent_confidence=conf,
            agent_reasoning=reasoning,
            created_at=_now(-3200),
            updated_at=_now(-600),
        )
        session.add(bump)
        bumps[key] = bump

    session.flush()

    # ---- Screenshot comparisons for decided PRs ----
    for key, baseline_fn, new_fn, decision, conf, reasoning in [
        ("ghostty",  "ghostty",          "ghostty",          "approve",       0.94, bumps["ghostty"].agent_reasoning),
        ("thrive",   "thrive",           "thrive",           "reject",        0.91, bumps["thrive"].agent_reasoning),
    ]:
        before_bytes, after_bytes = screenshots[key]
        comp = ScreenshotComparison(
            version_bump_pr_id=bumps[key].id,
            test_run_id=test_runs.get(key, {}) and test_runs[key].id if key in test_runs else None,
            baseline_image_b64=_b64(before_bytes),
            new_image_b64=_b64(after_bytes),
            decision=decision,
            confidence=conf,
            reasoning=reasoning,
            analyzed_at=_now(-700),
        )
        session.add(comp)

    session.flush()
    return bumps


# ---------------------------------------------------------------------------
# Stable screenshot baselines (used by the screenshot reviewer as "before")
# ---------------------------------------------------------------------------

def _seed_baselines(session, screenshots: dict[str, tuple[bytes, bytes]]) -> None:
    print("Seeding stable screenshot baselines…")
    for snap_name, (before_bytes, _) in screenshots.items():
        bl = StableScreenshotBaseline(
            user_id=USER_ID,
            snap_name=snap_name,
            architecture="amd64",
            image_name="main.png",
            image_b64=_b64(before_bytes),
            source_version={"ghostty": "v1.3.1", "thrive": "v0.9.2", "lemonade-desktop": "v10.3.0"}.get(snap_name, "latest"),
            promoted_at=_now(-86400),
        )
        session.add(bl)
    session.flush()


# ---------------------------------------------------------------------------
# Agent run history  — shows a furiously busy agent pool
# ---------------------------------------------------------------------------

def _seed_agent_runs(session) -> None:
    print("Seeding agent run history…")
    snaps = ["ghostty", "warble", "thrive", "gemini-desktop", "lemonade-desktop",
             "terminal-fun", "terminal-tetris", "terminal-2048", "terminal-solitaire"]

    history = [
        # (agent_type, snap_name, status, summary, delta_started, duration_s)
        ("release_scanner",    None,               "done",  "Scanned 80 snaps — 5 new releases found",                        -3800, 42),
        ("version_bumper",     "ghostty",          "done",  "Opened PR #5201: ghostty v1.3.1 → v1.3.2",                      -3600, 8),
        ("version_bumper",     "thrive",           "done",  "Opened PR #12: thrive v0.9.2 → v1.0.1",                         -3590, 7),
        ("version_bumper",     "warble",           "done",  "Opened PR #87: warble v2.0.1 → v2.2.0",                         -3580, 6),
        ("version_bumper",     "gemini-desktop",   "done",  "Opened PR #23: gemini-desktop 0.6.0 → 0.7.0",                   -3570, 9),
        ("pr_monitor",         "ghostty",          "done",  "CI passed — triggered YARF for ghostty v1.3.2",                  -3100, 5),
        ("pr_monitor",         "thrive",           "done",  "CI passed — triggered YARF for thrive v1.0.1",                   -3090, 4),
        ("pr_monitor",         "warble",           "done",  "CI passed — triggered YARF for warble v2.2.0",                   -3080, 5),
        ("pr_monitor",         "gemini-desktop",   "running", "Polling CI for gemini-desktop PR #23…",                        -900,  None),
        ("screenshot_reviewer","ghostty",          "done",  "ghostty: approve (confidence=94%)",                               -700, 18),
        ("screenshot_reviewer","thrive",           "done",  "thrive: reject (confidence=91%) — crash dialog detected",        -690, 21),
        ("stale_build_scanner",None,               "done",  "4 stale snaps found — dispatched rebuilds",                     -2400, 12),
        ("stale_build_scanner","terminal-fun",     "done",  "Dispatched rebuild: terminal-fun → candidate",                  -2395, 3),
        ("stale_build_scanner","terminal-2048",    "done",  "Dispatched rebuild: terminal-2048 → candidate",                 -2390, 3),
        ("stale_build_scanner","terminal-tetris",  "done",  "Dispatched rebuild: terminal-tetris → candidate",               -2385, 2),
        ("stale_build_scanner","terminal-solitaire","done", "Dispatched rebuild: terminal-solitaire → candidate",            -2380, 2),
        ("release_scanner",    None,               "done",  "Scanned 80 snaps — 1 new release found (lemonade-desktop)",     -1800, 38),
        ("version_bumper",     "lemonade-desktop", "done",  "Opened PR #41: lemonade-desktop v10.3.0 → v10.4.0",             -1650, 11),
        ("pr_monitor",         "lemonade-desktop", "running","Waiting for CI on lemonade-desktop PR #41…",                    -120,  None),
    ]

    for agent_type, snap_name, status, summary, delta_s, dur in history:
        started = _now(delta_s)
        finished = (started + timedelta(seconds=dur)) if dur is not None else None
        run = AgentRun(
            user_id=USER_ID,
            agent_type=agent_type,
            snap_name=snap_name,
            status=status,
            result_summary=summary if status in ("done", "error") else None,
            started_at=started,
            finished_at=finished,
        )
        session.add(run)

    session.flush()


# ---------------------------------------------------------------------------
# Stale build triggers
# ---------------------------------------------------------------------------

def _seed_stale_builds(session) -> None:
    print("Seeding stale build triggers…")
    for snap_name, snap_id, days in [
        ("terminal-fun",      49, 62),
        ("terminal-2048",     35, 88),
        ("terminal-tetris",   14, 54),
        ("terminal-solitaire",41, 71),
    ]:
        st = StaleBuildTrigger(
            snap_id=snap_id,
            user_id=USER_ID,
            packaging_repo=f"kenvandine/{snap_name}",
            channel="candidate",
            days_since_publish=days,
            status="triggered",
            triggered_at=_now(-2400),
        )
        session.add(st)
    session.flush()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    import importlib.util

    # Load screenshot generator from demo/screenshots.py
    spec = importlib.util.spec_from_file_location(
        "screenshots",
        os.path.join(os.path.dirname(__file__), "screenshots.py"),
    )
    sc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sc)

    print("Generating synthetic screenshots…")
    screenshots: dict[str, tuple[bytes, bytes]] = {
        "ghostty":          (sc.ghostty_before(),          sc.ghostty_after()),
        "thrive":           (sc.thrive_before(),           sc.thrive_after()),
        "lemonade-desktop": (sc.lemonade_desktop_before(), sc.lemonade_desktop_after()),
    }

    init_db()

    with get_session() as session:
        _clear(session)
        releases = _seed_upstream_releases(session)
        test_runs = _seed_test_runs(session)
        _seed_version_bump_prs(session, releases, test_runs, screenshots)
        _seed_baselines(session, screenshots)
        _seed_agent_runs(session)
        _seed_stale_builds(session)

    print("\nDemo database seeded successfully.")
    print("Start the server with:  SNAP_DASHBOARD_DEMO=1 snap-dashboard serve")


if __name__ == "__main__":
    main()
