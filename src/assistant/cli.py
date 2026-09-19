"""`pw` — the growing-assistant command line.

Implemented today: `status`, `doctor`, the `vault` group (`init`, `status`, `scan`) and the
knowledge commands (`reindex`, `search`). Commands that would need capability the project
does not have yet (`cases`, `tasks`, `scheduled`, `approve`, `run`) are deliberately not
registered, so the CLI never advertises behaviour that does not exist.

This module is the composition root: it is the one place allowed to build adapters and
store implementations from configuration and hand them to application services.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import platform
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from assistant import __version__
from assistant.adapters.content.registry import SuffixExtractorRegistry
from assistant.adapters.filesystem.scanner import FilesystemScanner
from assistant.adapters.filesystem.vault_manifest import VaultManifestFile
from assistant.adapters.knowledge.index_location import KnowledgeIndexLocator
from assistant.adapters.system_clock import SystemClock
from assistant.application.knowledge_indexer import KnowledgeIndexer
from assistant.application.knowledge_search import KnowledgeSearchService
from assistant.application.paths import AppPaths
from assistant.application.storage_catalog import StorageCatalogService
from assistant.domain.catalog import CatalogRoot, CatalogScanResult
from assistant.domain.errors import (
    DomainError,
    InvalidVaultManifest,
    StorageRootConflict,
    StorageRootIdentityMismatch,
    StorageRootOffline,
    UnknownStorageRoot,
    VaultAlreadyInitialized,
    VaultNotInitialized,
)
from assistant.domain.knowledge import KnowledgeIndexRunResult, KnowledgeSearchResult
from assistant.domain.vault import VaultManifest
from assistant.store.catalog import SqliteCatalogRepository
from assistant.store.db import Database
from assistant.store.errors import StoreError
from assistant.store.knowledge_index import (
    SqliteKnowledgeIndexFactory,
    sqlite_search_capabilities,
)
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
    capabilities = sqlite_search_capabilities()
    table.add_row(
        "sqlite FTS5",
        "ok" if capabilities.fts5 else f"[red]missing[/red] ({capabilities.detail})",
    )
    table.add_row(
        "FTS5 trigram",
        "ok" if capabilities.trigram else "[red]missing[/red]",
    )
    table.add_row("pypdf", _pypdf_version())
    console.print(table)

    if not python_ok:
        required = ".".join(str(part) for part in MINIMUM_PYTHON)
        _fail(f"pw requires Python >= {required}")
    if not (capabilities.fts5 and capabilities.trigram):
        _fail("knowledge search requires SQLite FTS5 with the trigram tokenizer")

    console.print("[green]environment looks usable[/green]")


def _pypdf_version() -> str:
    try:
        return importlib.metadata.version("pypdf")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - packaging edge case
        return "not installed"


@app.command()
def reindex(
    root: Annotated[
        str | None, typer.Option("--root", help="Only index this storage root id.")
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Re-extract even when metadata is unchanged.")
    ] = False,
) -> None:
    """Build or refresh the per-root knowledge index from the current catalog.

    This never scans: run `pw vault scan` first so newly added files are known.
    """
    root_ids = [root] if root is not None else [item.root.root_id for item in _catalog_roots()]
    if not root_ids:
        _fail("no storage roots in the catalog; run a catalog scan first")
    indexer = _build_indexer()
    failures = 0
    for root_id in root_ids:
        try:
            result = asyncio.run(indexer.index_root(root_id, force=force))
        except (StorageRootOffline, StorageRootIdentityMismatch) as exc:
            failures += 1
            error_console.print(f"[yellow]{root_id}: skipped — {exc}[/yellow]")
            continue
        except (UnknownStorageRoot, StoreError, DomainError) as exc:
            failures += 1
            error_console.print(f"[red]{root_id}: {exc}[/red]")
            continue
        _print_index_result(result)
    if failures:
        _fail(f"{failures} of {len(root_ids)} storage roots could not be indexed")


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Plain text to look for.")],
    root: Annotated[
        str | None, typer.Option("--root", help="Only search this storage root id.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Maximum hits per signal.")] = 10,
) -> None:
    """Search indexed content and catalog metadata for plain text."""
    service = _build_search_service()
    try:
        result = asyncio.run(service.search(query, root_id=root, limit=limit))
    except ValueError as exc:
        _fail(str(exc))
        return
    except StoreError as exc:
        _fail(f"search failed: {exc}")
        return
    _print_search_result(result)


def _build_indexer() -> KnowledgeIndexer:
    clock = SystemClock()
    catalog = SqliteCatalogRepository(_runtime_database(clock))
    return KnowledgeIndexer(
        catalog,
        SuffixExtractorRegistry(),
        SqliteKnowledgeIndexFactory(KnowledgeIndexLocator()),
        VaultManifestFile(clock),
        clock,
    )


def _build_search_service() -> KnowledgeSearchService:
    clock = SystemClock()
    return KnowledgeSearchService(
        SqliteCatalogRepository(_runtime_database(clock)),
        SqliteKnowledgeIndexFactory(KnowledgeIndexLocator()),
        VaultManifestFile(clock),
    )


def _runtime_database(clock: SystemClock) -> Database:
    database = Database.at(AppPaths.resolve().database_file)
    apply_migrations(database, clock=clock)
    return database


def _catalog_roots() -> list[CatalogRoot]:
    catalog = SqliteCatalogRepository(_runtime_database(SystemClock()))
    return asyncio.run(catalog.list_roots())


def _print_index_result(result: KnowledgeIndexRunResult) -> None:
    table = Table(
        title=f"knowledge index: {result.root_id}", show_header=False, title_justify="left"
    )
    table.add_row("seen", str(result.seen))
    table.add_row("indexed", str(result.indexed))
    table.add_row("empty", str(result.empty))
    table.add_row("unsupported", str(result.unsupported))
    table.add_row("skipped", str(result.skipped_unchanged))
    table.add_row("errors", str(result.errors))
    console.print(table)


def _print_search_result(result: KnowledgeSearchResult) -> None:
    for merged in result.content_hits:
        hit = merged.hit
        console.print(f"[bold]\\[content][/bold] {hit.logical_uri}")
        console.print(f"  {hit.source_span.describe()}")
        console.print(f"  {hit.snippet}")
    for entry in result.metadata_hits:
        console.print(f"[bold]\\[metadata][/bold] {entry.logical_uri}")
        console.print("  content unavailable / not indexed")
    if result.offline_roots:
        console.print("Offline roots skipped for content search:")
        for root_id in result.offline_roots:
            console.print(f"- {root_id}")
    if result.identity_mismatches:
        console.print("Roots whose mount holds a different vault:")
        for root_id in result.identity_mismatches:
            console.print(f"- {root_id}")
    if not result.content_hits and not result.metadata_hits:
        console.print("no matches")


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
