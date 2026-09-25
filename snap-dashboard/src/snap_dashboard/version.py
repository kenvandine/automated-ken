"""Runtime app version, shown at the bottom of the web UI so it's always
obvious exactly which commit is currently running.

When installed as a snap, snapd sets ``$SNAP_VERSION`` to whatever
``snapcraft.yaml``'s ``override-pull`` set the snap's version to at build
time — see that file's comments: CI stamps the commit that was actually
built (short SHA) onto the pyproject.toml version, e.g. ``0.1.0+gitabc1234``,
before ``snapcraft pack`` runs. That gives us an exact commit for free at
runtime with no extra packaging plumbing.

Outside a snap (e.g. running straight from a git checkout during
development), fall back to asking the local git checkout for its own HEAD.
"""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def get_app_version() -> str:
    snap_version = os.environ.get("SNAP_VERSION")
    if snap_version:
        return snap_version

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "dev"

    sha = result.stdout.strip()
    if result.returncode != 0 or not sha:
        return "dev"
    return f"dev+git{sha}"
