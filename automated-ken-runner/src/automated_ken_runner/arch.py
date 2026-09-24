"""Detect this machine's architecture in snapd's naming convention.

Reported at enrollment and on every heartbeat (see cli.py / runner.py) so
the dashboard can match each queued job's target architecture to a runner
that can actually run it (see snap_dashboard.web.routes.runner_api).
"""

from __future__ import annotations

import platform

# Mirrors snapd's `dpkg --print-architecture`-style naming — the same
# vocabulary already used for TestRun.architecture / ChannelMap.architecture
# on the dashboard side.
_MACHINE_TO_SNAP_ARCH = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "armv7l": "armhf",
    "armv6l": "armhf",
    "i686": "i386",
    "i386": "i386",
    "riscv64": "riscv64",
    "ppc64le": "ppc64el",
    "s390x": "s390x",
}


def detect_arch() -> str:
    """Return this machine's architecture in snapd naming (e.g. ``amd64``, ``arm64``).

    Falls back to the raw ``platform.machine()`` value for anything not in
    the known mapping, rather than guessing wrong.
    """
    machine = platform.machine().lower()
    return _MACHINE_TO_SNAP_ARCH.get(machine, machine)
