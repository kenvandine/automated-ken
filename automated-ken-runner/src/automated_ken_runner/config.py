"""Local persistence of this machine's enrollment credentials.

Stored at ``$SNAP_USER_COMMON/config.json`` when running as the
``automated-ken-runner`` snap (persists across snap refreshes), or
``~/.local/share/automated-ken-runner/config.json`` otherwise (mode 0600 —
this file contains the runner's bearer secret).
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path


def _config_dir() -> Path:
    snap_user_common = os.environ.get("SNAP_USER_COMMON")
    if snap_user_common:
        return Path(snap_user_common)
    xdg_data = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg_data) if xdg_data else Path.home() / ".local" / "share"
    return base / "automated-ken-runner"


def _config_path() -> Path:
    return _config_dir() / "config.json"


@dataclass
class RunnerConfig:
    server_url: str
    runner_id: int
    secret: str
    name: str = ""

    @property
    def api_base(self) -> str:
        return self.server_url.rstrip("/") + "/api/runners"


def save_config(config: RunnerConfig) -> Path:
    """Persist enrollment credentials, creating the directory with safe permissions."""
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(asdict(config), indent=2))
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600 — contains the bearer secret
    return path


def load_config() -> RunnerConfig | None:
    """Load enrollment credentials, or None if this machine isn't enrolled yet."""
    path = _config_path()
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return RunnerConfig(**data)


def clear_config() -> None:
    path = _config_path()
    if path.exists():
        path.unlink()
