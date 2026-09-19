"""`pw` — the growing-assistant command line.

Implemented today: `status`, `doctor`, and the `vault` group (`init`, `status`, `scan`).
Commands that would need capability the project does not have yet (`search`, `reindex`,
`cases`, `tasks`, `scheduled`, `approve`, `run`) are deliberately not registered, so the
CLI never advertises behaviour that does not exist.

This module is the composition root: it is the one place allowed to build adapters and
store implementations from configuration and hand them to application services.
"""

from __future__ import annotations

import asyncio
import platform
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from assistant import __version__
from assistant.adapters.filesystem.scanner import FilesystemScanner
from assistant.adapters.filesystem.vault_manifest import VaultManifestFile
from assistant.adapters.system_clock import SystemClock
from assistant.application.paths import AppPaths
from assistant.application.storage_catalog import StorageCatalogService
from assistant.domain.catalog import CatalogScanResult
from assistant.domain.errors import (
    DomainError,
    InvalidVaultManifest,
    StorageRootConflict,
    VaultAlreadyInitialized,
    VaultNotInitialized,
)
from assistant.domain.vault import VaultManifest
from assistant.store.catalog import SqliteCatalogRepository
from assistant.store.db import Database
from assistant.store.errors import StoreError
from assistant.store.migrations import apply_migrations

MINIMUM_PYTHON = (3, 13)

app = typer.Typer(
    help="pw — growing-assistant command line (durable event core; no integrations yet).",
    no_args_is_help=True,
    add_completion=False,
)
vault_app = typer.Typer(
    help="Archive Vault commands: identity and metadata catalog.", no_args_is_help=True
)
app.add_typer(vault_app, name="vault")

console = Console()
error_console = Console(stderr=True)


def _python_version() -> tuple[int, int, int]:
    return (sys.version_info.major, sys.version_info.minor, sys.version_info.micro)


def _in_virtualenv() -> bool:
    return sys.prefix != sys.base_prefix


def _fail(message: str, code: int = 1) -> None:
    """Report a user-facing failure and exit without a traceback."""
    error_console.print(f"[red]{message}[/red]")
    raise typer.Exit(code=code)


@app.command()
def status() -> None:
    """Show CLI status and what the daemon currently does."""
    table = Table(title="growing-assistant status", show_header=False, title_justify="left")
    table.add_row("version", __version__)
    table.add_row("core", "durable event pipeline (ingest, claim, retry, dead letter)")
    table.add_row("catalog", "storage identity + metadata catalog (no content index yet)")
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
        _fail(f"pw requires Python >= {required}")

    console.print("[green]environment looks usable[/green]")


@vault_app.command("init")
def vault_init(
    path: Annotated[Path, typer.Argument(help="Vault root; the directory must already exist.")],
    vault_id: Annotated[str, typer.Option("--id", help="Stable vault id, e.g. archive-main.")],
    label: Annotated[str, typer.Option("--label", help="Human-readable vault label.")],
) -> None:
    """Create `.pa/vault.toml`. Never scans and never touches the catalog."""
    try:
        manifest = asyncio.run(
            VaultManifestFile(SystemClock()).initialize(
                path, vault_id=vault_id, label=label
            )
        )
    except (VaultNotInitialized, VaultAlreadyInitialized, InvalidVaultManifest) as exc:
        _fail(str(exc))
        return
    console.print(f"[green]initialised vault[/green] {manifest.vault_id}")
    console.print(f"manifest: {path / '.pa' / 'vault.toml'}")


@vault_app.command("status")
def vault_status(
    path: Annotated[Path, typer.Argument(help="Vault root to inspect.")],
) -> None:
    """Show the vault manifest. This command never touches the database."""
    try:
        manifest = asyncio.run(VaultManifestFile(SystemClock()).read(path))
    except (VaultNotInitialized, InvalidVaultManifest) as exc:
        _fail(str(exc))
        return
    _print_manifest(manifest, path)


@vault_app.command("scan")
def vault_scan(
    path: Annotated[Path, typer.Argument(help="Vault root to scan into the host catalog.")],
) -> None:
    """Scan a vault and update the host metadata catalog.

    An incomplete scan is reported and exits non-zero: the metadata that was seen is
    stored, but missing detection is skipped because a partial view proves nothing.
    """
    clock = SystemClock()
    database = Database.at(AppPaths.resolve().database_file)
    try:
        apply_migrations(database, clock=clock)
        service = StorageCatalogService(
            FilesystemScanner(clock),
            VaultManifestFile(clock),
            SqliteCatalogRepository(database),
            clock,
        )
        result = asyncio.run(service.scan_vault(path))
    except (VaultNotInitialized, InvalidVaultManifest, StorageRootConflict) as exc:
        _fail(str(exc))
        return
    except StoreError as exc:
        _fail(f"catalog failure: {exc}")
        return
    _print_scan_result(result)
    if not result.scan_complete:
        _fail("scan incomplete — missing detection skipped", code=2)


def _print_manifest(manifest: VaultManifest, path: Path) -> None:
    table = Table(title="vault status", show_header=False, title_justify="left")
    table.add_row("vault id", manifest.vault_id)
    table.add_row("label", manifest.label)
    table.add_row("format version", str(manifest.format_version))
    table.add_row("path", str(path))
    console.print(table)


def _print_scan_result(result: CatalogScanResult) -> None:
    table = Table(title=f"vault scan: {result.root_id}", show_header=False, title_justify="left")
    table.add_row("seen", str(result.seen))
    table.add_row("created", str(result.created))
    table.add_row("updated", str(result.updated))
    table.add_row("unchanged", str(result.unchanged))
    table.add_row("restored", str(result.restored))
    table.add_row("missing", str(result.marked_missing))
    table.add_row("complete", "yes" if result.scan_complete else "[red]no[/red]")
    table.add_row("errors", str(result.errors))
    console.print(table)


def main() -> None:
    """Console-script entry point (`pw`)."""
    try:
        app()
    except DomainError as exc:  # pragma: no cover - defensive: commands report their own errors
        _fail(str(exc))


__all__ = ["app", "main"]

