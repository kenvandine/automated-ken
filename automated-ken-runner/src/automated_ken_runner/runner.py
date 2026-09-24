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

import io
import logging
import os
import shutil
import subprocess
import tempfile
import time
import traceback
import zipfile
from pathlib import Path
from typing import Callable

import httpx

from automated_ken_runner.arch import detect_arch
from automated_ken_runner.config import RunnerConfig
from automated_ken_runner.deps import ensure_dependencies
from automated_ken_runner.idle import is_safe_to_claim_job
from automated_ken_runner.screenshot_capture import ScreenshotCaptureError, capture_screenshot
from automated_ken_runner.screenshots import analyze_screenshot_png, extract_screenshots

logger = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL_SECONDS = 15
_POLL_TIMEOUT_SECONDS = 25
_IDLE_THRESHOLD_SECONDS = 120
# How long to let a newly-launched app finish rendering before screenshotting
# it in the generic (no-suite) desktop smoke test.
_APP_SETTLE_SECONDS = 15
_APP_LAUNCH_TIMEOUT_SECONDS = 30


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


class RunnerLoop:
    """Owns the long-lived connection to a snap-dashboard server."""

    def __init__(self, cfg: RunnerConfig) -> None:
        self.cfg = cfg
        self.client = httpx.Client(
            base_url=cfg.api_base,
            headers={"Authorization": f"Bearer {cfg.secret}"},
            timeout=_POLL_TIMEOUT_SECONDS + 10,
        )
        self._last_heartbeat = 0.0

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
        while True:
            try:
                self._maybe_heartbeat()
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

    def _maybe_heartbeat(self) -> None:
        now = time.monotonic()
        if now - self._last_heartbeat < _HEARTBEAT_INTERVAL_SECONDS:
            return
        safe = is_safe_to_claim_job(_IDLE_THRESHOLD_SECONDS)
        from automated_ken_runner.idle import get_idle_state

        state = get_idle_state()
        try:
            self.client.patch(
                f"/{self.cfg.runner_id}/heartbeat",
                json={
                    "status": "idle" if safe else "busy",
                    "idle_seconds": state.idle_seconds,
                    "locked": state.locked,
                    # Self-heal already-enrolled runners that predate arch
                    # reporting (see enroll() in cli.py) without requiring a
                    # manual re-enrollment — the server only overwrites its
                    # stored arch when this differs (see runner_api.py).
                    "arch": detect_arch(),
                },
            )
        except httpx.HTTPError as exc:
            logger.debug("Heartbeat failed (non-fatal): %s", exc)
        self._last_heartbeat = now

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
        snap_name = job["snap_name"]
        channel = job.get("channel", "stable")
        logger.info("Claimed job %s: %s (%s)", job_id, snap_name, channel)
        self._report_status(job_id, "running")

        log_lines: list[str] = []

        def _log(section: str, text: str) -> None:
            if text:
                log_lines.append(f"----- {section} -----\n{text.rstrip()}\n")

        with tempfile.TemporaryDirectory(prefix="automated-ken-runner-") as tmp:
            tmp_path = Path(tmp)
            try:
                suite_dir = self._fetch_suite(job_id, tmp_path)
                self._install_snap(snap_name, channel, _log)
                if suite_dir is not None:
                    # The packaging repo opted in to a custom Robot/YARF
                    # suite (real interaction beyond a plain smoke test) —
                    # keep using it as-is.
                    if shutil.which("yarf") is None:
                        # A dependency (installed at startup or via
                        # `prepare-machine`) has since gone missing — try
                        # once more to self-heal rather than failing every
                        # job with a bare FileNotFoundError until someone
                        # notices.
                        ensure_dependencies(auto_install=True)
                    yarf_exit, log_html = self._run_yarf(snap_name, suite_dir, tmp_path, _log)
                    shots = extract_screenshots(log_html) if log_html else []
                    passed = yarf_exit == 0 and all(s.is_valid for s in shots)
                    self._upload_screenshots(job_id, shots)
                    self._report_status(
                        job_id, "passed" if passed else "failed",
                        yarf_exit_code=yarf_exit, log="\n".join(log_lines),
                    )
                else:
                    # No suite configured for this repo — this is the
                    # common/default case for a plain desktop (GUI) app:
                    # launch it on the real desktop session, let it
                    # render, capture a screenshot natively, and do a
                    # basic sanity check. This is all generic, reusable
                    # logic that every packaging repo gets for free with
                    # no suite of its own to write or maintain — deeper
                    # pass/fail inference (LLM screenshot comparison
                    # against the stable baseline) happens dashboard-side
                    # once the screenshot is uploaded.
                    passed, shots = self._run_desktop_smoke_test(snap_name, tmp_path, _log)
                    self._upload_screenshots(job_id, shots)
                    self._report_status(
                        job_id, "passed" if passed else "failed", log="\n".join(log_lines),
                    )
            except Exception as exc:  # noqa: BLE001 — never crash the loop over one bad job
                logger.exception("Job %s failed with an unexpected error", job_id)
                _log("traceback", traceback.format_exc())
                self._report_status(job_id, "failed", error=str(exc), log="\n".join(log_lines))

    def _fetch_suite(self, job_id: int, tmp_path: Path) -> Path | None:
        """Fetch and unzip this job's suite, or None if the repo has none.

        A suite is optional — see ``_execute_job()`` — so a 404 here just
        means "run the generic desktop smoke test instead," not a failure.
        """
        resp = self.client.get(f"/{self.cfg.runner_id}/jobs/{job_id}/suite")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        suite_dir = tmp_path / "suite"
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            zf.extractall(suite_dir)
        return suite_dir

    def _install_snap(self, snap_name: str, channel: str, log: Callable[[str, str], None]) -> None:
        subprocess.run(
            ["snap", "info", snap_name], capture_output=True, timeout=30, check=False
        )
        installed = subprocess.run(
            ["snap", "list", snap_name], capture_output=True, timeout=15, check=False
        ).returncode == 0
        action = "refresh" if installed else "install"
        proc = subprocess.run(
            ["sudo", "snap", action, snap_name, f"--channel={channel}"],
            capture_output=True, timeout=300, check=False,
        )
        log(
            f"snap {action} {snap_name}",
            (proc.stdout or b"").decode(errors="replace") + (proc.stderr or b"").decode(errors="replace"),
        )
        proc.check_returncode()

    def _run_yarf(
        self, snap_name: str, suite_dir: Path, tmp_path: Path, log: Callable[[str, str], None]
    ) -> tuple[int, str]:
        outdir = tmp_path / "results"
        outdir.mkdir(exist_ok=True)
        # yarf only accepts "Mir" (real Wayland display, via WAYLAND_DISPLAY)
        # or "Vnc" (headless). Use Mir whenever a real graphical session is
        # present on this machine, otherwise fall back to yarf's own
        # "Vnc" default for headless runners.
        live_env = _live_session_env()
        platform_name = "Mir" if live_env.get("WAYLAND_DISPLAY") or live_env.get("DISPLAY") else "Vnc"
        # automated-ken-runner is a classic-confinement snap and sets
        # PYTHONHOME/PYTHONPATH for its own bundled interpreter. yarf is a
        # strictly-confined snap with its own Python — inheriting these
        # vars makes it try (and get AppArmor-denied) to read this
        # snap's site-packages. Strip them so yarf uses its own env.
        yarf_env = {
            k: v for k, v in live_env.items() if k not in ("PYTHONHOME", "PYTHONPATH")
        }
        proc = subprocess.run(
            ["yarf", "--platform", platform_name, "--outdir", str(outdir), str(suite_dir)],
            capture_output=True, timeout=1800, check=False, env=yarf_env,
        )
        log(
            f"yarf --platform {platform_name}",
            (proc.stdout or b"").decode(errors="replace") + (proc.stderr or b"").decode(errors="replace"),
        )
        log_html_path = outdir / "log.html"
        log_html = log_html_path.read_text(errors="replace") if log_html_path.exists() else ""
        return proc.returncode, log_html

    def _run_desktop_smoke_test(
        self, snap_name: str, tmp_path: Path, log: Callable[[str, str], None]
    ) -> tuple[bool, list]:
        """Generic "launch a GUI app, capture a screenshot" flow.

        This is the default test for any packaging repo with no custom
        suite of its own — it needs no per-repo test code at all. Launches
        ``snap_name`` on the runner's real desktop session, waits for it to
        appear and settle, takes one native screenshot (see
        ``screenshot_capture``), and does the same basic
        brightness/blank-frame sanity check YARF-sourced screenshots get
        (see ``screenshots.analyze_screenshot_png``) — deeper pass/fail
        inference (LLM comparison against the stable baseline) happens
        dashboard-side once the screenshot is uploaded.
        """
        app_proc = subprocess.Popen(
            ["snap", "run", snap_name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=_live_session_env(),
        )
        try:
            self._wait_for_app_alive(snap_name, timeout=_APP_LAUNCH_TIMEOUT_SECONDS)
            time.sleep(_APP_SETTLE_SECONDS)
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
                app_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                app_proc.kill()
                app_proc.wait(timeout=5)
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
