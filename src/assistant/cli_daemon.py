"""`pw daemon status`: is a daemon running for this runtime root? (ADR-0032).

The answer comes from the same OS lock `assistantd` takes, so it is a fact rather than a guess: the
command tries the advisory lock, sees whether somebody holds it, and releases it again. Nothing is
created, nothing is started, no socket is opened and no metadata is trusted as authorization — the
pid and start time written beside the lock are labelled informational, because a pid in a file is
not evidence that a process is alive.
"""

from __future__ import annotations

import typer
from rich.table import Table

from assistant import bootstrap
from assistant.adapters.runtime.instance_lock import daemon_lock_path, inspect_lock
from assistant.cli_support import console

daemon_app = typer.Typer(
    help="Daemon lifecycle diagnostics (local only).", no_args_is_help=True
)


@daemon_app.command("status")
def daemon_status() -> None:
    """Report whether an assistantd instance holds this runtime's lock. Local, read-only."""
    runtime = bootstrap.AppPaths.resolve().runtime
    lock_path = daemon_lock_path(runtime)
    state = inspect_lock(lock_path)
    table = Table(title="daemon status", show_header=False, title_justify="left")
    table.add_row("Running", "[green]yes[/green]" if state.held else "no")
    table.add_row("Runtime data directory", str(runtime))
    table.add_row("Lock file", lock_path.name + (" (held)" if state.held else " (not held)"))
    if state.metadata:
        table.add_row("PID metadata", _metadata_line(state.metadata))
    console.print(table)
    console.print(
        "The lock is an OS advisory lock: it is released automatically when the daemon process "
        "exits. PID metadata is informational and is never used to decide whether a daemon runs."
    )


def _metadata_line(metadata: dict[str, object]) -> str:
    """Render the informational header without exposing anything but identity and time."""
    ordered = ("pid", "started_at", "version")
    parts = [
        f"{key}={metadata[key]}"
        for key in ordered
        if key in metadata and metadata[key] is not None
    ]
    return ", ".join(parts) if parts else "(unreadable)"


def register(app: typer.Typer) -> None:
    """Register the `daemon` group on the root app."""
    app.add_typer(daemon_app, name="daemon")


__all__ = ["daemon_app", "register"]
