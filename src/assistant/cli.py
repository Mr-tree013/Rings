"""`pw` — the growing-assistant command line.

Implements only `status` and `doctor`. Commands that would need real capability
(`search`, `reindex`, `cases`, `tasks`, `scheduled`, `approve`, `run`) are deliberately
not registered yet, so the CLI never advertises behaviour that does not exist.
"""

from __future__ import annotations

import platform
import sys

import typer
from rich.console import Console
from rich.table import Table

from assistant import __version__
from assistant.application.paths import AppPaths

MINIMUM_PYTHON = (3, 13)

app = typer.Typer(
    help="pw — growing-assistant command line (durable event core; no integrations yet).",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
error_console = Console(stderr=True)


def _python_version() -> tuple[int, int, int]:
    return (sys.version_info.major, sys.version_info.minor, sys.version_info.micro)


def _in_virtualenv() -> bool:
    return sys.prefix != sys.base_prefix


@app.command()
def status() -> None:
    """Show CLI status and what the daemon currently does."""
    table = Table(title="growing-assistant status", show_header=False, title_justify="left")
    table.add_row("version", __version__)
    table.add_row("core", "durable event pipeline (ingest, claim, retry, dead letter)")
    table.add_row("cli", "[green]ok[/green]")
    table.add_row("daemon", "skeleton only (starts, waits, exits cleanly)")
    table.add_row(
        "integrations", "[yellow]not implemented[/yellow] (mail, index, web, scheduler, eHall)"
    )
    console.print(table)
    console.print("No external integration is wired up yet.")


@app.command()
def doctor() -> None:
    """Check the runtime environment and report diagnostics."""
    version = _python_version()
    version_text = ".".join(str(part) for part in version)
    python_ok = version[:2] >= MINIMUM_PYTHON
    paths = AppPaths.resolve()

    table = Table(title="pw doctor", show_header=False, title_justify="left")
    table.add_row("python", f"{version_text} ({'ok' if python_ok else 'too old'})")
    table.add_row("executable", sys.executable)
    table.add_row("platform", f"{platform.system()} {platform.release()} ({platform.machine()})")
    table.add_row("virtualenv", "yes" if _in_virtualenv() else "no")
    for label, path in paths.items():
        table.add_row(f"{label} dir", f"{path} ({'present' if path.exists() else 'absent'})")
    database_file = paths.database_file
    table.add_row(
        "runtime db",
        f"{database_file} ({'present' if database_file.exists() else 'absent'})",
    )
    console.print(table)

    if not python_ok:
        required = ".".join(str(part) for part in MINIMUM_PYTHON)
        error_console.print(f"[red]pw requires Python >= {required}[/red]")
        raise typer.Exit(code=1)

    console.print("[green]environment looks usable[/green]")


def main() -> None:
    """Console-script entry point (`pw`)."""
    app()
