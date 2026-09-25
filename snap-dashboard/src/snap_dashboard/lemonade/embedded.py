"""Bundled/embedded Lemonade server — a private, per-instance local LLM server.

Rather than depending on a system-wide ``lemonade-server`` install (whose
reachability/version/model selection is entirely outside our control), we
download and manage "Embeddable Lemonade" — a portable ``lemond`` binary
release published by https://github.com/lemonade-sdk/lemonade — as a
subprocess bound to a private, localhost-only port with a random API key
known only to this snap-dashboard instance.

This gives every agent that needs a vision-capable LLM (screenshot review,
auto-promotion) a server we fully own the lifecycle of: we control the
version, the port, the model, and whether it's running at all. No other
app or user can reach it, and it doesn't depend on the host having any
lemonade software installed.

An explicit ``UserConfig.lemonade_server_url`` (Settings -> Agents & AI)
still overrides this for anyone who wants to point at their own, more
powerful, self-managed lemonade-server instead.
"""

from __future__ import annotations

import logging
import os
import platform
import secrets
import subprocess
import tarfile
import tempfile
import threading
import time
from pathlib import Path

import httpx

from snap_dashboard.config import get_config, save_config
from snap_dashboard.lemonade.models import TASK_CONTEXT_SIZES, TASK_MODELS, TASK_TEXT

logger = logging.getLogger(__name__)

_RELEASES_API = "https://api.github.com/repos/lemonade-sdk/lemonade/releases/latest"
_HEALTH_TIMEOUT_SECONDS = 90
_HEALTH_POLL_INTERVAL = 2
_PULL_TIMEOUT_SECONDS = 1800  # multi-gigabyte model weights can take a while

# Kept for backward compatibility with anything importing the old single
# default model name — opinionated defaults now vary per task, see
# ``lemonade.models.TASK_MODELS``.
DEFAULT_EMBEDDED_MODEL = TASK_MODELS[TASK_TEXT]


def get_lemonade_data_dir() -> Path:
    """Return the directory embeddable Lemonade is installed/run from.

    Uses $SNAP_COMMON, not $SNAP_DATA: the downloaded lemond binary and
    model weights can be sizeable, and $SNAP_DATA is per-revision, so
    every refresh would otherwise leave a duplicate copy behind in the
    old revision's directory until it's pruned.
    """
    snap_common = os.environ.get("SNAP_COMMON")
    if snap_common:
        base = Path(snap_common) / "lemonade"
    else:
        base = Path.home() / ".local" / "share" / "snap-dashboard" / "lemonade"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _platform_asset_suffix() -> str | None:
    """Return the embeddable-release asset suffix for this machine, or None if unsupported."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system != "linux":
        # snap-dashboard itself only ships/targets Linux (it's a snap); the
        # embeddable Lemonade release also has macOS/Windows builds, but
        # there's no reason for this code path to run there today.
        return None
    if machine in ("x86_64", "amd64"):
        return "ubuntu-x64"
    if machine in ("aarch64", "arm64"):
        return "ubuntu-arm64"
    return None


def _find_release_asset(token: str = "") -> tuple[str, str] | None:
    """Return ``(version, download_url)`` for the current platform's embeddable asset."""
    suffix = _platform_asset_suffix()
    if not suffix:
        logger.warning("Embedded Lemonade: unsupported platform (%s/%s)", platform.system(), platform.machine())
        return None

    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(_RELEASES_API, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        logger.warning("Embedded Lemonade: failed to query latest release: %s", exc)
        return None

    version = data.get("tag_name", "").lstrip("v")
    for asset in data.get("assets", []):
        name = asset.get("name", "")
        if name.startswith("lemonade-embeddable-") and name.endswith(f"-{suffix}.tar.gz"):
            return version, asset.get("browser_download_url", "")
    logger.warning("Embedded Lemonade: no matching asset found for suffix %r", suffix)
    return None


def _install_dir_for_version(version: str) -> Path:
    return get_lemonade_data_dir() / "embeddable" / version


def _download_and_extract(url: str, dest_dir: Path) -> bool:
    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            with httpx.stream("GET", url, timeout=300, follow_redirects=True) as resp:
                resp.raise_for_status()
                for chunk in resp.iter_bytes(chunk_size=1 << 20):
                    tmp.write(chunk)
        # Extract next to dest_dir (not the system /tmp) so the final
        # rename() is same-filesystem: under a snap, /tmp is a private
        # tmpfs mount distinct from $SNAP_COMMON, and a cross-device
        # rename() raises EXDEV ("Invalid cross-device link").
        with tempfile.TemporaryDirectory(dir=dest_dir.parent) as extract_tmp:
            with tarfile.open(tmp_path) as tf:
                tf.extractall(extract_tmp)  # noqa: S202 — trusted release asset from our own API query
            # The tarball has a single top-level directory; move its contents in.
            entries = list(Path(extract_tmp).iterdir())
            if len(entries) == 1 and entries[0].is_dir():
                entries[0].rename(dest_dir)
            else:
                Path(extract_tmp).rename(dest_dir)
        return True
    except (httpx.HTTPError, OSError, tarfile.TarError) as exc:
        logger.warning("Embedded Lemonade: download/extract failed: %s", exc)
        return False
    finally:
        try:
            tmp_path.unlink(missing_ok=True)  # type: ignore[union-attr]
        except (NameError, OSError):
            pass


def ensure_installed(github_token: str = "") -> Path | None:
    """Ensure the embeddable Lemonade release is downloaded; return its ``lemond`` path."""
    found = _find_release_asset(github_token)
    if not found:
        return None
    version, url = found
    if not url:
        return None

    install_dir = _install_dir_for_version(version)
    lemond_path = install_dir / "lemond"
    if lemond_path.exists():
        return lemond_path

    logger.info("Embedded Lemonade: installing v%s to %s", version, install_dir)
    if not _download_and_extract(url, install_dir):
        return None
    lemond_path.chmod(0o755)
    (install_dir / "lemonade").chmod(0o755) if (install_dir / "lemonade").exists() else None
    return lemond_path if lemond_path.exists() else None


class EmbeddedLemonadeManager:
    """Owns the lifecycle of our private ``lemond`` subprocess."""

    def __init__(self) -> None:
        cfg = get_config()
        self.port = cfg.lemonade_embedded_port
        self.api_key = cfg.lemonade_embedded_api_key or self._generate_and_persist_api_key()
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._start_attempted = False
        self._pulled_models: set[str] = set()
        self._pull_lock = threading.Lock()

    @staticmethod
    def _generate_and_persist_api_key() -> str:
        key = secrets.token_hex(24)
        save_config({"LEMONADE_EMBEDDED_API_KEY": key})
        logger.info("Embedded Lemonade: generated and persisted a new private API key.")
        return key

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def ensure_started(self, github_token: str = "") -> bool:
        """Start the embedded server if it isn't already running. Returns True if healthy."""
        with self._lock:
            if self.is_running():
                return True
            self._start_attempted = True

            lemond_path = ensure_installed(github_token)
            if not lemond_path:
                logger.warning(
                    "Embedded Lemonade: could not install lemond — agents needing "
                    "a vision model will fall back to heuristics."
                )
                return False

            data_dir = get_lemonade_data_dir()
            cache_dir = data_dir / "cache"
            config_dir = data_dir / "config"
            cache_dir.mkdir(parents=True, exist_ok=True)
            config_dir.mkdir(parents=True, exist_ok=True)
            log_path = data_dir / "lemond.log"

            env = dict(os.environ)
            env["LEMONADE_API_KEY"] = self.api_key
            # Shadow GNU tar with our bundled bsdtar (see snapcraft.yaml,
            # which symlinks $SNAP/bin/tar -> bsdtar): GNU tar 1.35+ uses
            # the fchmodat2() syscall while extracting archives that
            # contain symlinks (e.g. lib*.so -> lib*.so.N in llama.cpp's
            # ROCm backend release), which core24's strict-mode seccomp
            # profile blocks, silently breaking `lemond`'s own internal
            # `backends install <recipe>:rocm` extraction and leaving only
            # CPU/Vulkan backends installed. bsdtar doesn't hit this gap.
            snap_dir = os.environ.get("SNAP")
            if snap_dir:
                bin_dir = f"{snap_dir}/bin"
                env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

            try:
                log_fh = open(log_path, "ab")
                self._proc = subprocess.Popen(  # noqa: S603
                    [
                        str(lemond_path),
                        str(cache_dir),
                        str(config_dir),
                        "--port", str(self.port),
                        "--host", "127.0.0.1",
                    ],
                    env=env,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as exc:
                logger.warning("Embedded Lemonade: failed to launch lemond: %s", exc)
                return False

        healthy = self._wait_until_healthy()
        if healthy:
            logger.info(
                "Embedded Lemonade: running on 127.0.0.1:%d (pid=%s)",
                self.port, self._proc.pid if self._proc else "?",
            )
            # Warm up every opinionated per-task default model (vision, text,
            # coding) in the background so the first real request for any
            # task type isn't a cold, multi-gigabyte pull, and so each is
            # loaded with its opinionated context size at least once.
            for task, model in TASK_MODELS.items():
                self.ensure_model_pulled(model, ctx_size=TASK_CONTEXT_SIZES.get(task))
        return healthy

    def _wait_until_healthy(self, timeout: int = _HEALTH_TIMEOUT_SECONDS) -> bool:
        deadline = time.monotonic() + timeout
        headers = {"Authorization": f"Bearer {self.api_key}"}
        while time.monotonic() < deadline:
            if not self.is_running():
                return False
            try:
                with httpx.Client(timeout=5) as client:
                    resp = client.get(f"{self.base_url}/v1/health", headers=headers)
                if resp.status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(_HEALTH_POLL_INTERVAL)
        return False

    def ensure_model_pulled(self, model: str, ctx_size: int | None = None) -> None:
        """Kick off a best-effort background pull+load of ``model`` if not already done.

        Safe to call repeatedly/concurrently for the same or different
        models -- each distinct model name is only ever pulled once per
        process lifetime. Downloads can be multi-gigabyte, so this always
        runs off the request path; if it hasn't finished by the time an
        agent needs the model, the chat/vision call just takes longer for
        that first request (lemond pulls-on-demand) rather than failing.

        When ``ctx_size`` is given, also asks lemond to load the model with
        that context window and persist it (``save_options: true``) so
        later loads — including lemond's own on-demand ones — keep using
        it without every caller having to pass ``ctx_size`` on every
        request. Sized generously by task (see
        ``lemonade.models.TASK_CONTEXT_SIZES``) since 128GB of unified RAM
        affords far more headroom than these models' small built-in
        defaults.
        """
        if not model:
            return
        with self._pull_lock:
            if model in self._pulled_models:
                return
            self._pulled_models.add(model)
        threading.Thread(target=self._pull_model, args=(model, ctx_size), daemon=True).start()

    def _pull_model(self, model: str, ctx_size: int | None = None) -> None:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            with httpx.Client(timeout=_PULL_TIMEOUT_SECONDS) as client:
                client.post(f"{self.base_url}/v1/pull", json={"model_name": model}, headers=headers)
                if ctx_size:
                    client.post(
                        f"{self.base_url}/v1/load",
                        json={"model_name": model, "ctx_size": ctx_size, "save_options": True},
                        headers=headers,
                    )
        except httpx.HTTPError as exc:
            logger.info("Embedded Lemonade: background model pull for %s did not complete: %s", model, exc)

    def stop(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.terminate()
                try:
                    # Kept short — this runs synchronously during app
                    # shutdown (web/app.py's on_shutdown), stacking on top
                    # of uvicorn's own connection-drain wait. lemond
                    # normally exits within a second or two of SIGTERM; if
                    # it hasn't after 5s, just kill it rather than let a
                    # slow/stuck subprocess make a snap refresh feel stuck.
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            self._proc = None


_manager: EmbeddedLemonadeManager | None = None
_manager_lock = threading.Lock()


def get_embedded_manager() -> EmbeddedLemonadeManager:
    """Return the process-wide embedded Lemonade manager singleton."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = EmbeddedLemonadeManager()
        return _manager
