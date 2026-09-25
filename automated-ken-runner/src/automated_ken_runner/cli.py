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
        Check the local prerequisites this runner needs: snapd, xterm
        (for console-app snaps), and the desktop idle-detection tooling
        (loginctl/gdbus). Also configures the desktop session for
        unattended GUI testing — installs/enables the screenshot-capture
        extension, enables autologin, disables screen lock/blanking —
        and reboots if any of that changed (see desktop_setup.py). The
        run loop also performs the dependency check (not the desktop
        setup) automatically at startup, so already-enrolled runners
        self-heal on their next service restart without needing this
        run by hand.
"""

from __future__ import annotations

import logging
import subprocess
import sys

import click
import httpx

from automated_ken_runner.arch import detect_arch
from automated_ken_runner.config import RunnerConfig, clear_config, load_config, save_config
from automated_ken_runner.deps import ensure_dependencies
from automated_ken_runner.desktop_setup import ensure_desktop_ready
from automated_ken_runner.idle import get_idle_state, is_safe_to_claim_job
from automated_ken_runner.runner import RunnerLoop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


@click.group()
def main() -> None:
    """automated-ken-runner — remote desktop smoke-test execution agent."""


@main.command()
@click.option("--server", required=True, help="Base URL of the snap-dashboard server.")
@click.option("--token", required=True, help="One-time enrollment token from the Runners page.")
@click.option("--name", default="", help="Human-friendly name for this machine (default: hostname).")
def enroll(server: str, token: str, name: str) -> None:
    """Enroll this machine with a snap-dashboard server."""
    import socket

    name = name or socket.gethostname()
    arch = detect_arch()
    try:
        resp = httpx.post(
            f"{server.rstrip('/')}/api/runners/enroll",
            json={"token": token, "name": name, "arch": arch},
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
    click.echo(f"Enrolled as runner #{cfg.runner_id} ({name}, {arch}). Credentials saved to {path}.")
    click.echo(
        "Start the service with: "
        "systemctl --user enable --now snap.automated-ken-runner.run.service"
    )


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
@click.option(
    "--reboot/--no-reboot", default=True,
    help="Reboot automatically if a desktop-setup change needs a fresh session to take "
    "effect (default: on — with autologin enabled the machine comes back up ready with "
    "nobody at the keyboard).",
)
def prepare_machine(reboot: bool) -> None:
    """Check (and auto-install where possible) prerequisites this runner needs.

    Also configures the desktop session itself for unattended GUI
    testing: installs/enables the screenshot-capture extension, turns on
    autologin, and disables screen lock/blanking (see
    ``desktop_setup.py``). Safe to re-run any time — every step is a
    no-op on an already-configured machine.
    """
    ok = ensure_dependencies(echo=click.echo, auto_install=True)
    click.echo("\nDesktop session setup:")
    needs_reboot = ensure_desktop_ready(echo=click.echo)
    if not ok:
        sys.exit(1)
    if not needs_reboot:
        click.echo("\nAll prerequisites found, desktop already configured.")
        return
    if not reboot:
        click.echo(
            "\nDesktop setup changed — a reboot is needed before the screenshot extension "
            "and/or autologin take effect. Run 'sudo reboot' when ready, or re-run "
            "'prepare-machine' without --no-reboot."
        )
        return
    click.echo("\nDesktop setup changed — rebooting now so it takes effect...")
    subprocess.run(["sudo", "reboot"], check=False)


if __name__ == "__main__":
    main()
