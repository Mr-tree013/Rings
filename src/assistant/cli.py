"""`pw` — the growing-assistant command line.

Implemented today: `status`, `doctor`, `roots list`, `sync`, the `vault` group
(`init`, `status`, `scan`), the knowledge commands (`reindex`, `search`), the commitment
commands (`task*`, `calendar`, `plan*`, `work*`), the scheduler views (`notifications*`,
`scheduled`) and the model boundary (`model status`, `model test`). Commands that would need
capability the project does not have yet (`cases`, `approve`, `run`) are deliberately not
registered, so the CLI never advertises behaviour that does not exist.

`pw sync` is the orchestrator: it reconciles the configured roots exactly as the daemon's
periodic loop does. The lower-level commands stay available for debugging.

This module is a composition root: it builds adapters and store implementations from
configuration (via `assistant.bootstrap`) and hands them to application services.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import platform
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from assistant import __version__, bootstrap, cli_commitments, cli_model, cli_scheduler
from assistant.adapters.filesystem.vault_manifest import manifest_path_for
from assistant.application.index_sync import IndexSyncResult, RootSyncResult, RootSyncStatus
from assistant.cli_support import console, error_console, fail
from assistant.domain.catalog import CatalogScanResult
from assistant.domain.config import AssistantConfig
from assistant.domain.errors import (
    ConfiguredRootNotFound,
    DomainError,
    InvalidAssistantConfig,
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
from assistant.store.errors import StoreError
from assistant.store.knowledge_index import sqlite_search_capabilities

MINIMUM_PYTHON = (3, 13)
FAILING_SYNC_STATUSES = frozenset(
    {
        RootSyncStatus.INCOMPLETE,
        RootSyncStatus.IDENTITY_MISMATCH,
        RootSyncStatus.INDEX_ERROR,
        RootSyncStatus.SCAN_ERROR,
    }
)

app = typer.Typer(
    help="pw — growing-assistant command line (durable event core; no integrations yet).",
    no_args_is_help=True,
    add_completion=False,
)
vault_app = typer.Typer(
    help="Archive Vault commands: identity and metadata catalog.", no_args_is_help=True
)
roots_app = typer.Typer(help="Configured storage roots.", no_args_is_help=True)
app.add_typer(vault_app, name="vault")
app.add_typer(roots_app, name="roots")
cli_commitments.register(app)
cli_scheduler.register(app)
cli_model.register(app)

_fail = fail
"""Backwards-compatible alias: the shared helper lives in `assistant.cli_support`."""


def _python_version() -> tuple[int, int, int]:
    return (sys.version_info.major, sys.version_info.minor, sys.version_info.micro)


def _in_virtualenv() -> bool:
    return sys.prefix != sys.base_prefix


def _load_config_or_fail() -> AssistantConfig:
    try:
        return asyncio.run(bootstrap.config_loader().load())
    except InvalidAssistantConfig as exc:
        _fail(f"invalid configuration: {exc}", code=2)
        raise AssertionError("unreachable") from exc


def _pypdf_version() -> str:
    try:
        return importlib.metadata.version("pypdf")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - packaging edge case
        return "not installed"


def _model_diagnostic(config: AssistantConfig) -> tuple[str, str]:
    """Report the model boundary without contacting the provider.

    A missing `[model]` section is not a problem: the model is an optional capability, exactly
    like the planner's. A configured provider without a credential *is* a problem worth
    failing on, because the user asked for something this host cannot do yet.
    """
    if config.model is None:
        return "not configured", "not needed"
    present = bootstrap.model_api_key() is not None
    return (
        f"configured ({config.model.provider}, {config.model.model})",
        "present" if present else "[red]missing[/red]",
    )


def _scheduler_store_available(database_file: Path) -> bool:
    """Read-only capability probe: are the scheduler tables migrated in?

    `pw doctor` never runs a job, creates a job or writes a notification; it only looks at the
    schema, and treats a missing or unreadable database as "not ready yet" instead of failing.
    """
    if not database_file.exists():
        return False
    try:
        database = bootstrap.runtime_database(bootstrap.system_clock())
        with database.connect() as connection:
            row = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name IN ('scheduled_jobs', 'notifications')"
            ).fetchall()
    except (StoreError, OSError):
        return False
    return len(row) == 2


@app.command()
def status() -> None:
    """Show static capability status (this command does not contact the daemon)."""
    table = Table(title="growing-assistant status", show_header=False, title_justify="left")
    table.add_row("version", __version__)
    table.add_row("core", "durable event pipeline (ingest, claim, retry, dead letter)")
    table.add_row("knowledge", "configured storage + per-root full-text index")
    table.add_row("commitments", "durable tasks, deadlines, calendar events, plan blocks, work")
    table.add_row("planning", "deterministic weekly proposals (review before apply)")
    table.add_row(
        "model", "provider-independent boundary (DeepSeek adapter); structured output validated"
    )
    table.add_row("interpreter", "[yellow]not implemented[/yellow]")
    table.add_row(
        "daemon services", "index-sync (periodic reconciliation), scheduler (jobs)"
    )
    table.add_row("cli", "[green]ok[/green]")
    table.add_row(
        "integrations", "[yellow]not implemented[/yellow] (mail, web, push, eHall)"
    )
    console.print(table)
    console.print(
        "Reminders are delivered into the durable notification inbox "
        "(read them with `pw notifications`)."
    )


@app.command()
def doctor() -> None:
    """Check the runtime environment and report diagnostics (read-only)."""
    version = _python_version()
    version_text = ".".join(str(part) for part in version)
    python_ok = version[:2] >= MINIMUM_PYTHON
    paths = bootstrap.AppPaths.resolve()
    config_error: str | None = None
    configured_roots = 0
    model_row: tuple[str, str] = ("not configured", "not needed")
    loader = bootstrap.config_loader()
    try:
        config = asyncio.run(loader.load())
        configured_roots = len(config.roots)
    except InvalidAssistantConfig as exc:
        config_error = str(exc)

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
    table.add_row("FTS5 trigram", "ok" if capabilities.trigram else "[red]missing[/red]")
    table.add_row("pypdf", _pypdf_version())
    if config_error is None:
        table.add_row("config", f"{loader.path} ({configured_roots} roots)")
        table.add_row(
            "scheduler store",
            (
                "due jobs + notification inbox (read-only check)"
                if _scheduler_store_available(database_file)
                else "[yellow]migration 0006 not applied yet[/yellow]"
            ),
        )
        table.add_row(
            "reminder offsets",
            ", ".join(str(offset) for offset in config.reminders.deadline_offsets_minutes)
            or "none configured",
        )
        table.add_row(
            "scheduler poll",
            (
                f"{config.scheduler.poll_interval_seconds}s "
                f"(replan debounce {config.scheduler.replan_debounce_seconds}s)"
            ),
        )
        model_row = _model_diagnostic(config)
        table.add_row("model config", model_row[0])
        table.add_row("model API key", model_row[1])
    else:
        table.add_row("config", f"[red]ERROR[/red] ({config_error})")
    console.print(table)

    if not python_ok:
        required = ".".join(str(part) for part in MINIMUM_PYTHON)
        _fail(f"pw requires Python >= {required}")
    if not (capabilities.fts5 and capabilities.trigram):
        _fail("knowledge search requires SQLite FTS5 with the trigram tokenizer")
    if config_error is not None:
        _fail("configuration is invalid")
    if model_row[0] != "not configured" and "missing" in model_row[1]:
        _fail(
            f"model is configured but {bootstrap.MODEL_API_KEY_ENV} is missing from the "
            "environment"
        )

    console.print("[green]environment looks usable[/green]")


@roots_app.command("list")
def roots_list() -> None:
    """List configured storage roots (config only; nothing is scanned)."""
    config = _load_config_or_fail()
    if not config.roots:
        console.print("No storage roots configured.")
        return
    table = Table(title=f"configured storage roots — {bootstrap.config_loader().path}")
    table.add_column("ID")
    table.add_column("Kind")
    table.add_column("Enabled")
    table.add_column("Configured path")
    for root in config.roots:
        table.add_row(root.root_id, root.kind.value, "yes" if root.enabled else "no", root.path)
    console.print(table)


@app.command()
def sync(
    root: Annotated[
        str | None, typer.Option("--root", help="Only reconcile this storage root id.")
    ] = None,
    force_index: Annotated[
        bool,
        typer.Option("--force-index", help="Re-extract even when metadata is unchanged."),
    ] = False,
) -> None:
    """Reconcile configured roots: catalog scan, then knowledge index."""
    config = _load_config_or_fail()
    if not config.roots:
        console.print("No configured roots.")
        return
    clock = bootstrap.system_clock()
    try:
        database = bootstrap.runtime_database(clock)
        service = bootstrap.sync_service(config, clock, database)
        result = asyncio.run(service.sync_once(root, force_index=force_index))
    except ConfiguredRootNotFound as exc:
        _fail(str(exc))
        return
    except StoreError as exc:
        _fail(f"index sync failed: {exc}")
        return
    _print_sync_result(result)
    failing = [item for item in result.roots if item.status in FAILING_SYNC_STATUSES]
    if failing:
        _fail(f"{len(failing)} of {result.configured} storage roots need attention")


def _print_sync_result(result: IndexSyncResult) -> None:
    for item in result.roots:
        _print_root_sync(item)
    console.print(
        f"totals: configured={result.configured} synced={result.synced} "
        f"offline={result.offline} incomplete={result.incomplete} failed={result.failed}"
    )


def _print_root_sync(item: RootSyncResult) -> None:
    console.print(f"[bold]{item.root_id}[/bold] ({item.kind.value})")
    console.print(f"  status: {item.status.value}")
    if item.catalog_result is not None:
        catalog = item.catalog_result
        console.print(
            f"  catalog: seen={catalog.seen} created={catalog.created} "
            f"updated={catalog.updated} unchanged={catalog.unchanged} "
            f"restored={catalog.restored} missing={catalog.marked_missing} "
            f"complete={'yes' if catalog.scan_complete else 'no'}"
        )
    if item.knowledge_result is not None:
        knowledge = item.knowledge_result
        console.print(
            f"  knowledge: indexed={knowledge.indexed} empty={knowledge.empty} "
            f"unsupported={knowledge.unsupported} skipped={knowledge.skipped_unchanged} "
            f"errors={knowledge.errors}"
        )
    if item.status is RootSyncStatus.INCOMPLETE:
        console.print("  knowledge indexing skipped")
    if item.error is not None:
        console.print(f"  error: {item.error}")


@app.command()
def reindex(
    root: Annotated[
        str | None, typer.Option("--root", help="Only index this storage root id.")
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Re-extract even when metadata is unchanged.")
    ] = False,
) -> None:
    """Build or refresh a knowledge index from the current catalog.

    This never scans: run `pw vault scan` (or `pw sync`) first so new files are known.
    """
    clock = bootstrap.system_clock()
    try:
        database = bootstrap.runtime_database(clock)
        catalog = bootstrap.catalog_repository(database)
        root_ids = (
            [root]
            if root is not None
            else [item.root.root_id for item in asyncio.run(catalog.list_roots())]
        )
        if not root_ids:
            _fail("no storage roots in the catalog; run a catalog scan first")
        indexer = bootstrap.knowledge_indexer(clock, database)
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
    except StoreError as exc:
        _fail(f"knowledge index failed: {exc}")
        return
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
    clock = bootstrap.system_clock()
    try:
        database = bootstrap.runtime_database(clock)
        service = bootstrap.search_service(clock, database)
        result = asyncio.run(service.search(query, root_id=root, limit=limit))
    except ValueError as exc:
        _fail(str(exc))
        return
    except StoreError as exc:
        _fail(f"search failed: {exc}")
        return
    _print_search_result(result)


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
    clock = bootstrap.system_clock()
    try:
        manifest = asyncio.run(
            bootstrap.VaultManifestFile(clock).initialize(
                path, vault_id=vault_id, label=label
            )
        )
    except (VaultNotInitialized, VaultAlreadyInitialized, InvalidVaultManifest) as exc:
        _fail(str(exc))
        return
    console.print(f"[green]initialised vault[/green] {manifest.vault_id}")
    console.print(f"manifest: {manifest_path_for(path)}")


@vault_app.command("status")
def vault_status(
    path: Annotated[Path, typer.Argument(help="Vault root to inspect.")],
) -> None:
    """Show the vault manifest. This command never touches the database."""
    clock = bootstrap.system_clock()
    try:
        manifest = asyncio.run(bootstrap.VaultManifestFile(clock).read(path))
    except (VaultNotInitialized, InvalidVaultManifest) as exc:
        _fail(str(exc))
        return
    _print_manifest(manifest, path)


@vault_app.command("scan")
def vault_scan(
    path: Annotated[Path, typer.Argument(help="Vault root to scan into the host catalog.")],
) -> None:
    """Scan a vault and update the host metadata catalog.

    An incomplete scan is reported and exits non-zero: the metadata that was seen is stored,
    but missing detection is skipped because a partial view proves nothing.
    """
    clock = bootstrap.system_clock()
    try:
        database = bootstrap.runtime_database(clock)
        service = bootstrap.catalog_service(clock, database)
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
    table = Table(title="vault scan", show_header=False, title_justify="left")
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
