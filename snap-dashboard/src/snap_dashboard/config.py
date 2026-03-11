"""Configuration management for snap-dashboard.

Reads config from environment variables first, then falls back to
$SNAP_DATA/config.env if $SNAP_DATA is set, else
~/.local/share/snap-dashboard/config.env.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _get_config_file_path() -> Path | None:
    snap_data = os.environ.get("SNAP_DATA")
    if snap_data:
        return Path(snap_data) / "config.env"
    return Path.home() / ".local" / "share" / "snap-dashboard" / "config.env"


def _load_env_file(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE file, ignoring blank lines and comments."""
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


def _get_value(key: str, file_values: dict[str, str], default: str) -> str:
    """Return env var if set, else file value, else default."""
    if key in os.environ:
        return os.environ[key]
    if key in file_values:
        return file_values[key]
    return default


@dataclass
class Config:
    bind: str = "127.0.0.1"
    port: int = 9080
    github_token: str = ""
    publisher: str = ""
    collect_interval_hours: int = 6


def get_config() -> Config:
    """Load and return the application configuration."""
    config_path = _get_config_file_path()
    file_values: dict[str, str] = {}
    if config_path is not None:
        file_values = _load_env_file(config_path)

    bind = _get_value("BIND", file_values, "127.0.0.1")
    port_str = _get_value("PORT", file_values, "9080")
    github_token = _get_value("GITHUB_TOKEN", file_values, "")
    publisher = _get_value("PUBLISHER", file_values, "")
    interval_str = _get_value("COLLECT_INTERVAL_HOURS", file_values, "6")

    try:
        port = int(port_str)
    except ValueError:
        port = 9080

    try:
        interval = int(interval_str)
    except ValueError:
        interval = 6

    return Config(
        bind=bind,
        port=port,
        github_token=github_token,
        publisher=publisher,
        collect_interval_hours=interval,
    )


def save_config(updates: dict[str, str]) -> None:
    """Write updated key/value pairs to the config file."""
    config_path = _get_config_file_path()
    if config_path is None:
        return
    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing = _load_env_file(config_path)
    existing.update(updates)
    lines = [f"{k}={v}" for k, v in existing.items()]
    config_path.write_text("\n".join(lines) + "\n")
