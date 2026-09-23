"""automate-ken-runner — remote YARF test execution agent.

Installed on an idle desktop/laptop with a real, logged-in graphical
session. Polls a snap-dashboard server for queued test jobs, runs YARF
against the real desktop (no synthetic compositor needed), and reports
status + screenshots back over HTTPS (outbound only — works behind NAT).

See REMOTE_RUNNER_PLAN.md in the automated-ken repo for the full design.
"""

from __future__ import annotations

__version__ = "0.1.0"
