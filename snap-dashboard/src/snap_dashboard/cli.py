"""CLI entry point for snap-dashboard."""

from __future__ import annotations

import sys
from datetime import timezone

import click

from snap_dashboard.config import get_config
from snap_dashboard.db.session import get_session, init_db


@click.group()
def main() -> None:
    """snap-dashboard — personal snap maintenance dashboard."""
    init_db()


@main.command()
def collect() -> None:
    """Run the data collector once and print a summary."""
    from snap_dashboard.collector import run_collection

    config = get_config()
    with get_session() as session:
        summary = run_collection(session, config)

    click.echo(f"Status:         {summary['status']}")
    click.echo(f"Snaps updated:  {summary['snaps_updated']}")
    click.echo(f"Issues fetched: {summary['issues_updated']}")
    if summary.get("error"):
        click.echo(f"Error:          {summary['error']}", err=True)
        sys.exit(1)


@main.command()
@click.option("--port", default=None, type=int, help="Port to listen on.")
@click.option("--bind", default=None, help="Bind address.")
def serve(port: int | None, bind: str | None) -> None:
    """Start the web server."""
    import uvicorn

    config = get_config()
    listen_port = port or config.port
    listen_bind = bind or config.bind

    uvicorn.run(
        "snap_dashboard.web.app:app",
        host=listen_bind,
        port=listen_port,
        reload=False,
        log_level="info",
    )


@main.command(name="add")
@click.argument("snap_name")
@click.option("--packaging-repo", default=None, help="Packaging repository URL.")
@click.option("--upstream-repo", default=None, help="Upstream repository URL.")
@click.option("--notes", default=None, help="Free-form notes.")
def add_snap(
    snap_name: str,
    packaging_repo: str | None,
    upstream_repo: str | None,
    notes: str | None,
) -> None:
    """Add a snap to the tracked list."""
    from snap_dashboard.db.models import Snap

    with get_session() as session:
        existing = session.query(Snap).filter_by(name=snap_name).first()
        if existing:
            click.echo(f"Snap {snap_name!r} is already tracked.")
            return

        config = get_config()
        snap = Snap(
            name=snap_name,
            publisher=config.publisher or "",
            manually_added=True,
            packaging_repo=packaging_repo,
            upstream_repo=upstream_repo,
            notes=notes,
        )
        session.add(snap)
    click.echo(f"Added snap {snap_name!r}.")


@main.command(name="remove")
@click.argument("snap_name")
def remove_snap(snap_name: str) -> None:
    """Stop tracking a snap."""
    from snap_dashboard.db.models import Snap

    with get_session() as session:
        snap = session.query(Snap).filter_by(name=snap_name).first()
        if not snap:
            click.echo(f"Snap {snap_name!r} not found.", err=True)
            sys.exit(1)
        session.delete(snap)
    click.echo(f"Removed snap {snap_name!r}.")


@main.command(name="list")
def list_snaps() -> None:
    """Print all tracked snaps with their channel versions."""
    from snap_dashboard.db.models import ChannelMap, Snap

    with get_session() as session:
        snaps = session.query(Snap).order_by(Snap.name).all()
        if not snaps:
            click.echo("No snaps tracked yet. Run 'collect' or 'add' first.")
            return

        # Header
        click.echo(
            f"{'Snap':<30} {'Stable':<20} {'Candidate':<20} {'Beta':<20} {'Edge':<20}"
        )
        click.echo("-" * 110)

        for snap in snaps:
            channels: dict[str, str] = {}
            for cm in (
                session.query(ChannelMap)
                .filter_by(snap_id=snap.id, architecture="amd64")
                .all()
            ):
                channels[cm.channel] = cm.version or "?"

            stable = channels.get("stable", "-")
            candidate = channels.get("candidate", "-")
            beta = channels.get("beta", "-")
            edge = channels.get("edge", "-")

            flag = " [manual]" if snap.manually_added else ""
            click.echo(
                f"{snap.name + flag:<30} {stable:<20} {candidate:<20} {beta:<20} {edge:<20}"
            )
