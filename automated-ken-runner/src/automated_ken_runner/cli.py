"""CLI entry point for automated-ken-runner.

Commands:
    automated-ken-runner enroll --server URL --token TOKEN [--name NAME]
        One-time enrollment against a snap-dashboard server's enrollment
        token (generated from the dashboard's Runners page), persists the
        resulting bearer secret locally.

    automated-ken-runner run
        Long-running poll/execute/report loop. Intended to run as a
        systemd --user service (see systemd/automated-ken-runner.service)
        under the same graphical session it's meant to test against.

    automated-ken-runner status
        Print enrollment + idle-detection status and exit.

    automated-ken-runner prepare-machine
        Best-effort check (and where possible, install) of the local
        prerequisites this runner needs: snapd, YARF, and the desktop
        idle-detection tooling (loginctl/gdbus).
"""

from __future__ import annotations

import logging
import shutil
import sys
import time

import click
import httpx

from automated_ken_runner.config import RunnerConfig, clear_config, load_config, save_config
from automated_ken_runner.idle import get_idle_state, is_safe_to_claim_job
from automated_ken_runner.runner import RunnerLoop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


@click.group()
def main() -> None:
    """automated-ken-runner — remote YARF test execution agent."""


@main.command()
@click.option("--server", required=True, help="Base URL of the snap-dashboard server.")
@click.option("--token", required=True, help="One-time enrollment token from the Runners page.")
@click.option("--name", default="", help="Human-friendly name for this machine (default: hostname).")
def enroll(server: str, token: str, name: str) -> None:
    """Enroll this machine with a snap-dashboard server."""
    import socket

    name = name or socket.gethostname()
    try:
        resp = httpx.post(
            f"{server.rstrip('/')}/api/runners/enroll",
            json={"token": token, "name": name},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        click.echo(f"Enrollment failed: {exc}", err=True)
        sys.exit(1)

    cfg = RunnerConfig(
        server_url=server,
        runner_id=data["runner_id"],
        secret=data["secret"],
        name=name,
    )
    path = save_config(cfg)
    click.echo(f"Enrolled as runner #{cfg.runner_id} ({name}). Credentials saved to {path}.")
    click.echo("Start the service with: systemctl --user enable --now automated-ken-runner")


@main.command()
def status() -> None:
    """Print enrollment and idle-detection status."""
    cfg = load_config()
    if cfg is None:
        click.echo("Not enrolled. Run `automated-ken-runner enroll --server ... --token ...` first.")
    else:
        click.echo(f"Enrolled as runner #{cfg.runner_id} ({cfg.name}) -> {cfg.server_url}")

    state = get_idle_state()
    click.echo(f"Locked: {state.locked}  Idle seconds: {state.idle_seconds}")
    click.echo(f"Safe to claim a job right now: {is_safe_to_claim_job()}")


@main.command()
def unenroll() -> None:
    """Forget this machine's enrollment credentials."""
    clear_config()
    click.echo("Enrollment credentials cleared.")


@main.command()
def run() -> None:
    """Run the poll/execute/report loop until interrupted."""
    cfg = load_config()
    if cfg is None:
        click.echo("Not enrolled. Run `automated-ken-runner enroll` first.", err=True)
        sys.exit(1)
    RunnerLoop(cfg).run_forever()


@main.command(name="prepare-machine")
def prepare_machine() -> None:
    """Best-effort check of prerequisites this runner needs on a test laptop."""
    checks = {
        "snap": "Required to install/refresh the snaps under test.",
        "yarf": "The YARF test-automation tool that drives the app under test.",
        "loginctl": "Used for idle/lock detection (part of systemd, should always be present).",
    }
    missing = []
    for cmd, why in checks.items():
        found = shutil.which(cmd)
        status_str = found or "NOT FOUND"
        click.echo(f"  {cmd:12s} {status_str:30s} {why}")
        if not found:
            missing.append(cmd)

    if "yarf" in missing:
        click.echo(
            "\nYARF not found. Install it per https://yarf.readthedocs.io/ "
            "(this runner does not attempt to install it automatically since "
            "install methods vary by distro/environment)."
        )
    if missing:
        sys.exit(1)
    click.echo("\nAll prerequisites found.")


if __name__ == "__main__":
    main()
