"""The runner's poll -> claim -> execute -> report loop.

Talks to ``/api/runners/*`` on the snap-dashboard server (see
REMOTE_RUNNER_PLAN.md, Phases R2-R5, for the full protocol). This client
is written against that planned contract; the corresponding server-side
routes are tracked separately and may not exist on every snap-dashboard
version yet — network errors here are handled the same way regardless
(log + back off + retry), so this loop degrades gracefully against a
server that doesn't support remote runners at all.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Callable

import httpx

from automated_ken_runner.arch import detect_arch
from automated_ken_runner.config import RunnerConfig
from automated_ken_runner.deps import ensure_dependencies
from automated_ken_runner.idle import is_safe_to_claim_job
from automated_ken_runner.screenshot_capture import ScreenshotCaptureError, capture_screenshot
from automated_ken_runner.screenshots import analyze_screenshot_png

logger = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL_SECONDS = 15
_POLL_TIMEOUT_SECONDS = 25
_IDLE_THRESHOLD_SECONDS = 120
# How long to let a newly-launched app finish rendering before screenshotting
# it in the generic desktop smoke test.
_APP_SETTLE_SECONDS = 15
_APP_LAUNCH_TIMEOUT_SECONDS = 30
# Terminal emulator used to run "console app" snaps (see Snap.is_console_app)
# so their text UI actually renders on-screen for the screenshot instead of
# running headless with nothing to capture. xterm is used rather than
# gnome-terminal because it *is* the window process itself (killing it
# reliably closes the window); gnome-terminal is a client of a persistent
# gnome-terminal-server, so killing the launching process doesn't
# necessarily close the window it opened. xterm runs fine under GNOME's
# Wayland session via XWayland, which every stock Ubuntu GNOME desktop has.
_CONSOLE_TERMINAL = "xterm"


def _desktop_env() -> str:
    import os

    return os.environ.get("XDG_CURRENT_DESKTOP", "") or os.environ.get("DESKTOP_SESSION", "")


def _live_session_env() -> dict[str, str]:
    """This process's own env, patched with the *current* systemd --user
    manager environment for graphical-session variables.

    This service can be started (at boot, or by ``systemctl --user
    restart``) before the desktop session finishes importing DISPLAY/
    WAYLAND_DISPLAY into the systemd --user manager (that import happens
    once, typically via gnome-session, some time after login) — a
    long-running service's own ``os.environ`` never picks those up
    afterwards even though ``systemctl --user show-environment`` does.
    Confirmed root cause of yarf silently falling back to its headless
    "Vnc" platform (no VNC server running) on a real graphical runner,
    and would equally break ``snap run`` for the no-suite smoke test.
    """
    env = dict(os.environ)
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show-environment"],
            capture_output=True, timeout=10, check=False, text=True,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                key, sep, value = line.partition("=")
                if sep and key in (
                    "DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR",
                    "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS",
                ):
                    env[key] = value
    except Exception:
        logger.debug("Failed to query systemd --user environment; using process env as-is", exc_info=True)
    return env


class JobCancelled(Exception):
    """The dashboard asked for the in-flight job to stop (see /runners → Cancel)."""


class RunnerLoop:
    """Owns the long-lived connection to a snap-dashboard server."""

    def __init__(self, cfg: RunnerConfig) -> None:
        self.cfg = cfg
        self.client = httpx.Client(
            base_url=cfg.api_base,
            headers={"Authorization": f"Bearer {cfg.secret}"},
            timeout=_POLL_TIMEOUT_SECONDS + 10,
        )
        # Heartbeats run on their own thread (and client) so they keep
        # flowing while a job is executing — otherwise the dashboard marks
        # the runner offline mid-job and never hears about a cancel request.
        self._heartbeat_client = httpx.Client(
            base_url=cfg.api_base,
            headers={"Authorization": f"Bearer {cfg.secret}"},
            timeout=15,
        )
        self._current_job_id: int | None = None
        self._cancel = threading.Event()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run_forever(self) -> None:
        logger.info("Runner #%s (%s) starting poll loop against %s", self.cfg.runner_id, self.cfg.name, self.cfg.server_url)
        # Self-heal already-enrolled runners that are missing a
        # dependency (e.g. YARF) picked up after enrollment — this way
        # fixing it just means restarting the service, not re-running
        # `prepare-machine` by hand on every machine.
        ensure_dependencies(auto_install=True)
        threading.Thread(target=self._heartbeat_loop, daemon=True, name="heartbeat").start()
        while True:
            try:
                job = self._poll_next_job()
                if job is not None:
                    self._execute_job(job)
                else:
                    time.sleep(2)
            except httpx.HTTPError as exc:
                logger.warning("Runner loop network error, backing off: %s", exc)
                time.sleep(10)
            except KeyboardInterrupt:
                logger.info("Runner loop interrupted, exiting.")
                return

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def _heartbeat_loop(self) -> None:
        while True:
            try:
                self._heartbeat()
            except Exception:  # noqa: BLE001 — never let the heartbeat thread die
                logger.debug("Heartbeat failed (non-fatal)", exc_info=True)
            time.sleep(_HEARTBEAT_INTERVAL_SECONDS)

    def _heartbeat(self) -> None:
        busy = self._current_job_id is not None
        from automated_ken_runner.idle import get_idle_state

        state = get_idle_state()
        safe = not state.locked and state.idle_seconds is not None and state.idle_seconds >= _IDLE_THRESHOLD_SECONDS
        resp = self._heartbeat_client.patch(
            f"/{self.cfg.runner_id}/heartbeat",
            json={
                "status": "busy" if busy or not safe else "idle",
                "idle_seconds": state.idle_seconds,
                "locked": state.locked,
                # Self-heal already-enrolled runners that predate arch
                # reporting (see enroll() in cli.py) without requiring a
                # manual re-enrollment — the server only overwrites its
                # stored arch when this differs (see runner_api.py).
                "arch": detect_arch(),
            },
        )
        if busy and resp.status_code == 200 and resp.json().get("cancel_requested"):
            if not self._cancel.is_set():
                logger.info("Cancel requested for job %s", self._current_job_id)
            self._cancel.set()

    # ------------------------------------------------------------------
    # Job claiming
    # ------------------------------------------------------------------

    def _poll_next_job(self) -> dict | None:
        if not is_safe_to_claim_job(_IDLE_THRESHOLD_SECONDS):
            return None
        try:
            resp = self.client.get(
                f"/{self.cfg.runner_id}/next-job",
                params={"timeout": _POLL_TIMEOUT_SECONDS},
            )
        except httpx.TimeoutException:
            return None
        if resp.status_code == 204:
            return None
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _execute_job(self, job: dict) -> None:
        job_id = job["test_run_id"]
        self._cancel.clear()
        self._current_job_id = job_id
        try:
            self._run_job(job)
        finally:
            self._current_job_id = None

    def _run_job(self, job: dict) -> None:
        job_id = job["test_run_id"]
        snap_name = job["snap_name"]
        channel = job.get("channel", "stable")
        is_console_app = bool(job.get("is_console_app", False))
        logger.info(
            "Claimed job %s: %s (%s, %s)%s",
            job_id, snap_name, channel, job.get("architecture", ""),
            " [console app]" if is_console_app else "",
        )
        self._report_status(job_id, "running")

        log_lines: list[str] = []

        def _log(section: str, text: str) -> None:
            if text:
                log_lines.append(f"----- {section} -----\n{text.rstrip()}\n")

        with tempfile.TemporaryDirectory(prefix="automated-ken-runner-") as tmp:
            tmp_path = Path(tmp)
            try:
                self._install_snap(snap_name, channel, _log)
                self._check_cancelled()
                # Every snap gets the same generic smoke test now — no
                # per-repo Robot/YARF suite support. yarf has no working
                # platform against a real GNOME session anyway (see
                # REMOTE_RUNNER_PLAN.md's Phase R5 findings: its "Mir"
                # platform needs wlroots-only protocols Mutter doesn't
                # implement, and its "Vnc" platform has no local server
                # to talk to on a stock GNOME desktop). Launch the app,
                # let it render, capture a native screenshot, and do a
                # basic sanity check — this is all generic, reusable
                # logic every packaging repo gets for free with nothing
                # of its own to write or maintain. Deeper pass/fail
                # inference (LLM screenshot comparison against the
                # stable baseline) happens dashboard-side once the
                # screenshot is uploaded. Snaps whose UI is text/console
                # only (Snap.is_console_app) are launched inside a
                # terminal window instead of bare on the desktop, since
                # there'd otherwise be nothing to screenshot.
                passed, shots = self._run_desktop_smoke_test(
                    snap_name, is_console_app, tmp_path, _log
                )
                self._upload_screenshots(job_id, shots)
                self._report_status(
                    job_id, "passed" if passed else "failed", log="\n".join(log_lines),
                )
            except JobCancelled:
                logger.info("Job %s cancelled", job_id)
                self._report_status(job_id, "cancelled", log="\n".join(log_lines))
            except Exception as exc:  # noqa: BLE001 — never crash the loop over one bad job
                logger.exception("Job %s failed with an unexpected error", job_id)
                _log("traceback", traceback.format_exc())
                self._report_status(job_id, "failed", error=str(exc), log="\n".join(log_lines))

    def _install_snap(self, snap_name: str, channel: str, log: Callable[[str, str], None]) -> None:
        info = subprocess.run(
            ["snap", "info", snap_name], capture_output=True, timeout=30, check=False
        )
        needs_classic = self._snap_needs_classic(
            (info.stdout or b"").decode(errors="replace"), channel
        )
        installed = subprocess.run(
            ["snap", "list", snap_name], capture_output=True, timeout=15, check=False
        ).returncode == 0
        action = "refresh" if installed else "install"
        cmd = ["sudo", "snap", action, snap_name, f"--channel={channel}"]
        if needs_classic:
            cmd.append("--classic")
        proc = subprocess.run(cmd, capture_output=True, timeout=300, check=False)
        log(
            f"snap {action} {snap_name}",
            (proc.stdout or b"").decode(errors="replace") + (proc.stderr or b"").decode(errors="replace"),
        )
        proc.check_returncode()

    @staticmethod
    def _snap_needs_classic(snap_info_output: str, channel: str) -> bool:
        """Return True if ``snap info`` reports *channel* as classic confinement.

        Classic-confinement snaps (e.g. fresh-editor) refuse ``snap
        install``/``refresh`` without an explicit ``--classic`` flag —
        this reads that requirement straight from the store metadata
        every job already fetches, so no per-snap runner config is
        needed to know which ones need it.

        Modern ``snap info`` has no single top-level ``confinement:``
        field — confinement is only shown per line in the ``channels:``
        block, as a trailing ``classic`` word after the size, e.g.::

            channels:
              latest/stable:    0.4.6 2026-08-06 (10) 10.5MB classic
              latest/candidate: 0.5.1 2026-09-25 (14) 11.4MB classic

        so this looks at the specific ``<track>/<channel>`` line being
        installed (falling back to any channel line, then the
        ``installed:`` line, if that exact one is a "^" placeholder
        pointing at another channel or otherwise missing).
        """
        channel_lines: list[str] = []
        installed_line = ""
        in_channels = False
        for raw_line in snap_info_output.splitlines():
            if raw_line.startswith("channels:"):
                in_channels = True
                continue
            if raw_line.startswith("installed:"):
                in_channels = False
                installed_line = raw_line
                continue
            if in_channels:
                if not raw_line.startswith((" ", "\t")):
                    in_channels = False
                    continue
                stripped = raw_line.strip()
                track_channel, sep, rest = stripped.partition(":")
                if not sep:
                    continue
                risk = track_channel.rsplit("/", 1)[-1]
                if "^" in rest:
                    continue
                if risk == channel:
                    return "classic" in rest.split()
                channel_lines.append(rest)

        # Exact channel wasn't listed (e.g. it's a "^" alias for another
        # channel) — any channel line reflects the snap's confinement
        # just as well since it practically never varies by channel.
        for rest in channel_lines:
            return "classic" in rest.split()
        return "classic" in installed_line.split()


    def _run_desktop_smoke_test(
        self, snap_name: str, is_console_app: bool, tmp_path: Path, log: Callable[[str, str], None]
    ) -> tuple[bool, list]:
        """Generic "launch the app, capture a screenshot" flow.

        This is now the only test every packaging repo gets — it needs no
        per-repo test code at all. Launches ``snap_name`` on the runner's
        real desktop session, waits for it to appear and settle, takes one
        native screenshot (see ``screenshot_capture``), and does the same
        basic brightness/blank-frame sanity check
        ``screenshots.analyze_screenshot_png`` provides — deeper pass/fail
        inference (LLM comparison against the stable baseline) happens
        dashboard-side once the screenshot is uploaded.

        Console apps (``Snap.is_console_app``) have a text UI with nothing
        to screenshot when launched bare, so they're launched inside a
        terminal window (see ``_CONSOLE_TERMINAL``) instead — everything
        else about the flow (settle, screenshot, sanity check, teardown)
        is identical.
        """
        env = _live_session_env()
        if is_console_app:
            if shutil.which(_CONSOLE_TERMINAL) is None:
                log(
                    "console app launch",
                    f"{_CONSOLE_TERMINAL} is not installed on this runner — "
                    f"install it with 'sudo apt install {_CONSOLE_TERMINAL}'",
                )
                return False, []
            cmd = [_CONSOLE_TERMINAL, "-e", "snap", "run", snap_name]
        else:
            cmd = ["snap", "run", snap_name]
        # Logged unconditionally (not just on failure) — a launch that never
        # appears is often an interface that didn't auto-connect (wayland,
        # opengl, desktop, ...); having this in every job's log means that
        # doesn't require reproducing the failure to diagnose.
        conns = subprocess.run(
            ["snap", "connections", snap_name], capture_output=True, timeout=15, check=False,
        )
        log(
            "snap connections",
            (conns.stdout or b"").decode(errors="replace") + (conns.stderr or b"").decode(errors="replace"),
        )
        app_proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
        )
        try:
            self._wait_for_app_alive(snap_name, timeout=_APP_LAUNCH_TIMEOUT_SECONDS)
            if self._cancel.wait(_APP_SETTLE_SECONDS):
                raise JobCancelled()
            shot_path = tmp_path / "screenshot.png"
            try:
                raw_png = capture_screenshot(shot_path)
            except ScreenshotCaptureError as exc:
                log("screenshot capture", str(exc))
                return False, []
            shot = analyze_screenshot_png(raw_png, "screenshot-001.png")
            if shot is None:
                log("screenshot analysis", "captured screenshot could not be decoded as an image")
                return False, []
            return shot.is_valid, [shot]
        finally:
            app_proc.terminate()
            try:
                output, _ = app_proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                app_proc.kill()
                try:
                    output, _ = app_proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    output = b""
            # Captured regardless of pass/fail — a process that never
            # "appears" to pgrep (e.g. flash-cards/drawing timing out
            # above) still often prints why on stdout/stderr before dying,
            # and that was previously discarded unread.
            if output:
                log("snap run output", output.decode(errors="replace"))
            subprocess.run(["pkill", "-f", f"/snap/{snap_name}/"], check=False)

    @staticmethod
    def _wait_for_app_alive(snap_name: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if subprocess.run(
                ["pgrep", "-f", f"/snap/{snap_name}/"], capture_output=True, check=False
            ).returncode == 0:
                return
            time.sleep(1)
        raise RuntimeError(f"Timed out waiting for {snap_name} process to appear")

    def _check_cancelled(self) -> None:
        if self._cancel.is_set():
            raise JobCancelled()

    def _upload_screenshots(self, job_id: int, shots: list) -> None:
        for shot in shots:
            try:
                self.client.post(
                    f"/{self.cfg.runner_id}/jobs/{job_id}/screenshots",
                    files={"file": (shot.image_name, shot.png_bytes, "image/png")},
                    data={
                        "width": shot.width,
                        "height": shot.height,
                        "brightness_mean": shot.brightness_mean,
                        "is_valid": shot.is_valid,
                    },
                )
            except httpx.HTTPError as exc:
                logger.warning("Screenshot upload failed for %s: %s", shot.image_name, exc)

    def _report_status(self, job_id: int, status: str, **extra) -> None:
        try:
            self.client.patch(f"/{self.cfg.runner_id}/jobs/{job_id}", json={"status": status, **extra})
        except httpx.HTTPError as exc:
            logger.warning("Status report failed for job %s: %s", job_id, exc)
