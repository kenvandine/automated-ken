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
import platform
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

import httpx

from automated_ken_runner.config import RunnerConfig
from automated_ken_runner.idle import is_safe_to_claim_job
from automated_ken_runner.screenshots import extract_screenshots

logger = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL_SECONDS = 15
_POLL_TIMEOUT_SECONDS = 25
_IDLE_THRESHOLD_SECONDS = 120


def _desktop_env() -> str:
    import os

    return os.environ.get("XDG_CURRENT_DESKTOP", "") or os.environ.get("DESKTOP_SESSION", "")


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

        with tempfile.TemporaryDirectory(prefix="automated-ken-runner-") as tmp:
            tmp_path = Path(tmp)
            try:
                suite_dir = self._fetch_suite(job_id, tmp_path)
                self._install_snap(snap_name, channel)
                yarf_exit, log_html = self._run_yarf(snap_name, suite_dir, tmp_path)
                shots = extract_screenshots(log_html) if log_html else []
                passed = yarf_exit == 0 and all(s.is_valid for s in shots)
                self._upload_screenshots(job_id, shots)
                self._report_status(
                    job_id, "passed" if passed else "failed", yarf_exit_code=yarf_exit
                )
            except Exception as exc:  # noqa: BLE001 — never crash the loop over one bad job
                logger.exception("Job %s failed with an unexpected error", job_id)
                self._report_status(job_id, "failed", error=str(exc))

    def _fetch_suite(self, job_id: int, tmp_path: Path) -> Path:
        resp = self.client.get(f"/{self.cfg.runner_id}/jobs/{job_id}/suite")
        resp.raise_for_status()
        suite_dir = tmp_path / "suite"
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            zf.extractall(suite_dir)
        return suite_dir

    def _install_snap(self, snap_name: str, channel: str) -> None:
        subprocess.run(
            ["snap", "info", snap_name], capture_output=True, timeout=30, check=False
        )
        installed = subprocess.run(
            ["snap", "list", snap_name], capture_output=True, timeout=15, check=False
        ).returncode == 0
        action = "refresh" if installed else "install"
        subprocess.run(
            ["sudo", "snap", action, snap_name, f"--channel={channel}"],
            capture_output=True, timeout=300, check=True,
        )

    def _run_yarf(self, snap_name: str, suite_dir: Path, tmp_path: Path) -> tuple[int, str]:
        outdir = tmp_path / "results"
        outdir.mkdir(exist_ok=True)
        platform_name = "Wayland" if "wayland" in platform.uname().release.lower() else "X11"
        proc = subprocess.run(
            ["yarf", "--platform", platform_name, "--outdir", str(outdir), str(suite_dir)],
            capture_output=True, timeout=1800, check=False,
        )
        log_html_path = outdir / "log.html"
        log_html = log_html_path.read_text(errors="replace") if log_html_path.exists() else ""
        return proc.returncode, log_html

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
