#!/usr/bin/env python3
"""Playwright demo driver — records the full presentation video.

Prerequisites (run from snap-dashboard/ with .venv active):
    pip install playwright pillow
    playwright install chromium
    python demo/seed_demo.py              # seed the database once

Also requires dbus-python (system package):
    sudo apt install python3-dbus   # if not already installed

Start the server in a separate terminal FIRST:
    SNAP_DASHBOARD_DEMO=1 snap-dashboard serve

Then run this script:
    python demo/run_demo.py

Output: demo/output/demo.mp4  (30fps via GNOME Shell Screencast D-Bus API)

Works on both X11 and Wayland. The GNOME Shell Screencast API records the
compositor output directly, so it captures native Wayland windows correctly.

The demo runs for ~3.5 minutes and covers:
  - Summary dashboard with attention-needed snaps
  - Live agent activity feed
  - Version bumps list (approved / rejected / running)
  - ghostty PR detail: before/after screenshots, AI reasoning, merge
  - thrive PR detail: rejection with crash dialog
  - lemonade-desktop PR advancing through the full pipeline live
  - Settings page with Lemonade config
  - Auto-promotion completing, dashboard updated
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

BASE_URL = os.environ.get("SNAP_DASHBOARD_URL", "http://127.0.0.1:8080")
OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
VIDEO_PATH = str(OUTPUT_DIR)


def _workarea() -> tuple[int, int, int, int]:
    """Return (x, y, width, height) of the usable GNOME work area.

    Tries xprop (_NET_WORKAREA) first; falls back to full display size.
    Width and height are rounded DOWN to the nearest even number.
    """
    import subprocess, re
    try:
        out = subprocess.check_output(
            ["xprop", "-root", "_NET_WORKAREA"], text=True, timeout=3
        )
        nums = list(map(int, re.findall(r"\d+", out.split("=")[1])))
        x, y, w, h = nums[0], nums[1], nums[2], nums[3]
    except Exception:
        x, y, w, h = 0, 0, 1920, 1080
    w = w - (w % 2)
    h = h - (h % 2)
    return x, y, w, h


class GnomeScreencast:
    """Thin wrapper around org.gnome.Shell.Screencast D-Bus API.

    Works on Wayland (and X11). The D-Bus connection MUST stay alive for the
    entire recording — GNOME Shell stops recording when the sender disconnects.
    Keep an instance of this object alive until StopScreencast is called.
    """

    def __init__(self) -> None:
        try:
            import dbus
        except ImportError:
            print("ERROR: dbus-python not installed.\n"
                  "  sudo apt install python3-dbus")
            sys.exit(1)
        self._dbus = dbus
        self._bus = dbus.SessionBus()
        shell = self._bus.get_object(
            "org.gnome.Shell.Screencast", "/org/gnome/Shell/Screencast"
        )
        self._iface = dbus.Interface(shell, "org.gnome.Shell.Screencast")
        self._filename: str | None = None

    def start_area(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        output_path: str,
        framerate: int = 30,
    ) -> str:
        """Start recording a screen area. Returns the actual filename used."""
        d = self._dbus
        # Pass path without extension — GNOME appends .mp4 automatically
        template = str(Path(output_path).with_suffix(""))
        success, filename = self._iface.ScreencastArea(
            d.Int32(x), d.Int32(y), d.Int32(width), d.Int32(height),
            template,
            d.Dictionary(
                {"framerate": d.UInt32(framerate), "draw-cursor": d.Boolean(True)},
                signature="sv",
            ),
        )
        if not success:
            raise RuntimeError("GNOME Shell ScreencastArea returned failure")
        self._filename = str(filename)
        return self._filename

    def stop(self) -> None:
        """Stop the active recording."""
        ok = self._iface.StopScreencast()
        if not ok:
            print("  Warning: StopScreencast returned false (no active recording?)")

# ---------------------------------------------------------------------------
# Sync with simulator.py pipeline schedule:
#   t=18s  ci_pending
#   t=40s  ci_passed
#   t=65s  yarf_running
#   t=125s yarf_passed
#   t=162s agent_approved
#   t=192s stable_promoted
#
# Navigation plan (t = seconds from demo start):
#   t=0    open dashboard
#   t=6    scroll snap table
#   t=14   navigate /agents
#   t=24   navigate /version-bumps
#   t=32   click ghostty (agent_approved)
#   t=48   click Merge
#   t=56   back to version-bumps, navigate to thrive (rejected)
#   t=74   back to version-bumps
#   t=80   navigate /agents  (lemonade-desktop advancing live)
#   t=100  navigate /settings
#   t=114  navigate /agents again
#   t=128  navigate /version-bumps
#   t=136  click lemonade-desktop PR (wait for agent_approved)
#   t=165  back to /agents, ⚡ indicator firing
#   t=178  back to /version-bumps → wait for stable_promoted
#   t=195  navigate /  — lemonade-desktop in stable
#   t=205  END
# ---------------------------------------------------------------------------


def main() -> None:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        print("ERROR: playwright not installed.\n"
              "  pip install playwright && playwright install chromium")
        sys.exit(1)

    wa_x, wa_y, wa_w, wa_h = _workarea()
    print(f"Work area: {wa_w}x{wa_h} at ({wa_x},{wa_y})")

    output_path = OUTPUT_DIR / "demo.mp4"

    screencast = GnomeScreencast()

    demo_start = time.monotonic()

    def elapsed() -> float:
        return time.monotonic() - demo_start

    def wait_until(t: float, poll: float = 0.25) -> None:
        """Busy-wait until elapsed() >= t."""
        while elapsed() < t:
            time.sleep(poll)

    with sync_playwright() as pw:
        # --app removes the URL bar and tab strip so the content fills the full
        # window without browser chrome eating into the viewport height.
        # Position and size exactly match the GNOME work area so there are
        # no gray bars caused by panel/dock reservations.
        browser = pw.chromium.launch(
            executable_path="/snap/bin/google-chrome",
            headless=False,
            args=[
                f"--app={BASE_URL}",
                f"--window-size={wa_w},{wa_h}",
                f"--window-position={wa_x},{wa_y}",
            ],
        )
        context = browser.new_context(
            viewport={"width": wa_w, "height": wa_h},
        )
        page = context.new_page()

        def goto(path: str, wait_for: str | None = None) -> None:
            page.goto(f"{BASE_URL}{path}")
            if wait_for:
                try:
                    page.wait_for_selector(wait_for, timeout=8000)
                except PWTimeout:
                    pass

        def slow_scroll(distance: int = 400, steps: int = 12, delay: float = 0.08) -> None:
            for _ in range(steps):
                page.mouse.wheel(0, distance // steps)
                time.sleep(delay)

        def slow_scroll_up(steps: int = 8, delay: float = 0.06) -> None:
            for _ in range(steps):
                page.mouse.wheel(0, -60)
                time.sleep(delay)

        # ------------------------------------------------------------------
        # 0s — Log in, then start the screen recorder once Chrome is visible
        # ------------------------------------------------------------------
        print(f"[{elapsed():.1f}s] Logging in via /demo-login")
        goto("/demo-login")
        page.wait_for_url(f"{BASE_URL}/", timeout=6000)
        # Browser is on screen and settled — start the screen recorder now
        print(f"[{elapsed():.1f}s] Starting GNOME Shell screencast recorder")
        actual_file = screencast.start_area(wa_x, wa_y, wa_w, wa_h, str(output_path))
        print(f"  Recording to: {actual_file}")
        time.sleep(1.5)

        # ------------------------------------------------------------------
        # 0s — Summary dashboard
        # ------------------------------------------------------------------
        print(f"[{elapsed():.1f}s] Dashboard")
        # Wait for snap table to render
        page.wait_for_selector("table, .snap-card, .attention-card", timeout=8000)
        time.sleep(2.0)

        # Scroll down to show snap table
        slow_scroll(500, steps=16, delay=0.10)
        time.sleep(2.0)
        slow_scroll(400, steps=12, delay=0.10)
        time.sleep(1.5)
        slow_scroll_up(steps=10)
        time.sleep(1.0)

        # ------------------------------------------------------------------
        # ~14s — Agent activity feed
        # ------------------------------------------------------------------
        wait_until(14)
        print(f"[{elapsed():.1f}s] Navigating to /agents")
        goto("/agents", wait_for=".agent-feed, #activity-log, .pipeline-stats")
        time.sleep(2.5)
        slow_scroll(300, steps=10)
        time.sleep(2.0)
        slow_scroll(300, steps=10)
        time.sleep(1.5)
        slow_scroll_up(steps=8)
        time.sleep(1.5)

        # ------------------------------------------------------------------
        # ~24s — Version bumps list
        # ------------------------------------------------------------------
        wait_until(24)
        print(f"[{elapsed():.1f}s] Navigating to /version-bumps")
        goto("/version-bumps", wait_for=".bump-group, .version-bump-list, h2")
        time.sleep(2.0)
        slow_scroll(400, steps=14, delay=0.09)
        time.sleep(1.5)
        slow_scroll(400, steps=12)
        time.sleep(1.5)
        slow_scroll_up(steps=10)
        time.sleep(1.0)

        # ------------------------------------------------------------------
        # ~32s — ghostty PR detail (agent_approved)
        # ------------------------------------------------------------------
        wait_until(32)
        print(f"[{elapsed():.1f}s] Clicking ghostty PR")
        ghostty_link = page.locator("a:has-text('ghostty')").first
        try:
            ghostty_link.click(timeout=4000)
        except PWTimeout:
            # Fall back to direct URL if selector misses
            goto("/version-bumps/1", wait_for=".screenshot-comparison, .bump-detail")
        page.wait_for_selector(".screenshot-comparison, img, .bump-detail", timeout=6000)
        time.sleep(2.0)
        slow_scroll(300, steps=10)
        time.sleep(2.0)
        slow_scroll(300, steps=10)
        time.sleep(2.0)
        slow_scroll_up(steps=8)
        time.sleep(1.5)

        # ------------------------------------------------------------------
        # ~48s — Merge ghostty PR
        # ------------------------------------------------------------------
        wait_until(48)
        print(f"[{elapsed():.1f}s] Merging ghostty PR")
        merge_btn = page.locator("form[action*='merge'] button, button:has-text('Merge')")
        try:
            merge_btn.first.click(timeout=4000)
        except PWTimeout:
            print("  (merge button not found — continuing)")
        # Redirected to /version-bumps, wait for merged status
        try:
            page.wait_for_selector(".status-merged, :text('Merged')", timeout=6000)
        except PWTimeout:
            pass
        time.sleep(2.0)

        # ------------------------------------------------------------------
        # ~56s — thrive PR detail (agent_rejected)
        # ------------------------------------------------------------------
        wait_until(56)
        print(f"[{elapsed():.1f}s] Clicking thrive (rejected) PR")
        thrive_link = page.locator("a:has-text('thrive')").first
        try:
            thrive_link.click(timeout=4000)
        except PWTimeout:
            goto("/version-bumps/2", wait_for=".screenshot-comparison, .bump-detail")
        page.wait_for_selector(".screenshot-comparison, img, .bump-detail", timeout=6000)
        time.sleep(2.0)
        slow_scroll(300, steps=10)
        time.sleep(2.0)
        slow_scroll(300, steps=10)
        time.sleep(2.0)

        # ------------------------------------------------------------------
        # ~74s — Back to version-bumps
        # ------------------------------------------------------------------
        wait_until(74)
        print(f"[{elapsed():.1f}s] Back to /version-bumps")
        goto("/version-bumps", wait_for="h1, h2")
        time.sleep(2.0)

        # ------------------------------------------------------------------
        # ~80s — Agents feed (lemonade-desktop should be in ci_passed / yarf_running)
        # ------------------------------------------------------------------
        wait_until(80)
        print(f"[{elapsed():.1f}s] /agents — watching lemonade-desktop advance")
        goto("/agents", wait_for=".agent-feed, #activity-log")
        time.sleep(3.0)
        slow_scroll(400, steps=12)
        time.sleep(3.0)
        slow_scroll_up(steps=8)
        time.sleep(2.0)

        # ------------------------------------------------------------------
        # ~100s — Settings page showing Lemonade config
        # ------------------------------------------------------------------
        wait_until(100)
        print(f"[{elapsed():.1f}s] /settings — Lemonade config")
        goto("/settings", wait_for="form, .settings-form")
        time.sleep(2.0)
        slow_scroll(400, steps=12)
        time.sleep(2.5)
        slow_scroll(300, steps=10)
        time.sleep(2.0)
        slow_scroll_up(steps=10)
        time.sleep(1.5)

        # ------------------------------------------------------------------
        # ~114s — Back to agents (yarf_passed → screenshot reviewer should be firing soon)
        # ------------------------------------------------------------------
        wait_until(114)
        print(f"[{elapsed():.1f}s] /agents — awaiting ⚡ Lemonade AI activity")
        goto("/agents", wait_for=".agent-feed, #activity-log")
        time.sleep(4.0)
        slow_scroll(300, steps=10)
        time.sleep(3.0)
        slow_scroll_up(steps=6)
        time.sleep(3.0)

        # ------------------------------------------------------------------
        # ~128s — Version bumps — lemonade-desktop should appear as yarf_passed or agent_approved
        # ------------------------------------------------------------------
        wait_until(128)
        print(f"[{elapsed():.1f}s] /version-bumps — looking for lemonade-desktop")
        goto("/version-bumps", wait_for="h2")
        time.sleep(2.0)
        slow_scroll(300, steps=10)
        time.sleep(1.5)

        # ------------------------------------------------------------------
        # ~136s — lemonade-desktop PR detail (wait for agent_approved)
        # ------------------------------------------------------------------
        wait_until(136)
        print(f"[{elapsed():.1f}s] Clicking lemonade-desktop PR")
        ld_link = page.locator("a:has-text('lemonade-desktop')").first
        try:
            ld_link.click(timeout=4000)
        except PWTimeout:
            goto("/version-bumps/5", wait_for=".bump-detail, img")
        page.wait_for_selector(".screenshot-comparison, img, .bump-detail", timeout=8000)
        time.sleep(2.0)
        slow_scroll(300, steps=10)
        time.sleep(2.5)
        slow_scroll(300, steps=10)
        time.sleep(2.5)
        slow_scroll_up(steps=8)
        time.sleep(2.0)

        # ------------------------------------------------------------------
        # ~165s — Back to /agents — screenshot reviewer + promoter finishing
        # ------------------------------------------------------------------
        wait_until(165)
        print(f"[{elapsed():.1f}s] /agents — watching promotion activity")
        goto("/agents", wait_for=".agent-feed, #activity-log")
        time.sleep(4.0)
        slow_scroll(300, steps=10)
        time.sleep(3.0)
        slow_scroll_up(steps=6)
        time.sleep(2.0)

        # ------------------------------------------------------------------
        # ~178s — Version bumps — wait for stable_promoted
        # ------------------------------------------------------------------
        wait_until(178)
        print(f"[{elapsed():.1f}s] /version-bumps — waiting for stable_promoted")
        goto("/version-bumps", wait_for="h2")
        # Poll until we see stable_promoted or timeout at 200s
        while elapsed() < 200:
            try:
                page.wait_for_selector(
                    ".status-stable_promoted, :text('Stable Promoted'), :text('stable_promoted')",
                    timeout=3000,
                )
                break
            except PWTimeout:
                page.reload()
                time.sleep(2)
        time.sleep(3.0)
        slow_scroll(300, steps=10)
        time.sleep(2.0)

        # ------------------------------------------------------------------
        # ~195s — Dashboard — lemonade-desktop now showing new stable version
        # ------------------------------------------------------------------
        wait_until(195)
        print(f"[{elapsed():.1f}s] Back to dashboard")
        goto("/", wait_for="table, .snap-card")
        time.sleep(2.5)
        slow_scroll(300, steps=10)
        time.sleep(2.5)
        slow_scroll_up(steps=8)
        time.sleep(3.0)

        # ------------------------------------------------------------------
        # Done
        # ------------------------------------------------------------------
        print(f"[{elapsed():.1f}s] Demo complete — stopping recorder")
        screencast.stop()
        time.sleep(2)  # give GNOME Shell a moment to flush the file

        context.close()
        browser.close()

    print(f"\nVideo saved: {output_path}")


if __name__ == "__main__":
    main()
