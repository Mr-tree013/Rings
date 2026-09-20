"""`pw integrity` and `pw backup`: operational checks, backups and staging restore (ADR-0031).

```text
pw integrity check                    read-only, offline audit of the whole runtime
pw backup create FILE                 consistent snapshot + referenced objects, written atomically
pw backup verify FILE                 read-only verification of an existing archive
pw backup inspect FILE                the manifest only: versions, counts, hashes, migrations
pw backup restore FILE --to DIRECTORY staging restore; never in place
```

Exit codes: `0` valid/successful, `1` the archive or runtime is bad (or the request is refused by
policy, such as a non-empty destination) and `2` the command was invoked wrongly — a `FILE` that is
not a file is a usage problem, not a corrupt archive, and the two are worth telling apart in a
recovery runbook.

Two things here are deliberately absent. There is no `--in-place`, no `--force` and no automatic
backup daemon: a restore always targets a new directory, an existing archive is never overwritten,
and the only way a backup happens is that someone asks for one. And nothing in this module is ever
run by the daemon: these are operator commands.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.table import Table

from assistant import bootstrap
from assistant.application.backup_service import (
    BackupResult,
    BackupService,
    RestoreResult,
    VerificationResult,
)
from assistant.application.integrity_service import IntegrityService
from assistant.cli_support import console, fail, format_local
from assistant.domain.backup import ARCHIVE_SUFFIX, BackupManifest, sha256_hex
from assistant.domain.errors import (
    BackupSourceCorrupt,
    BackupSourceMissing,
    DomainError,
    IntegrityCheckFailed,
    InvalidBackupArchive,
    RestoreDestinationRejected,
)
from assistant.domain.integrity import IntegrityReport, IntegritySeverity
from assistant.store.errors import StoreError

ingest_app = typer.Typer(help="Integrity checks (read-only, offline).", no_args_is_help=True)
backup_app = typer.Typer(
    help="Operational backups and staging restore.", no_args_is_help=True
)

_EXPECTED_FAILURES = (
    BackupSourceCorrupt,
    BackupSourceMissing,
    IntegrityCheckFailed,
    InvalidBackupArchive,
    RestoreDestinationRejected,
)

_SEVERITY_STYLE = {
    IntegritySeverity.OK: "green",
    IntegritySeverity.WARN: "yellow",
    IntegritySeverity.FAIL: "red",
    IntegritySeverity.CRITICAL: "bold red",
}


def _run[T](action: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Run one async action, mapping project errors to CLI failures."""
    try:
        return asyncio.run(action())
    except _EXPECTED_FAILURES as exc:
        fail(str(exc))
    except DomainError as exc:
        fail(str(exc))
    except StoreError as exc:
        fail(f"store failure: {exc}")


def _require_archive(archive: Path) -> None:
    """Refuse a `FILE` argument that is not a file, as a usage error rather than a bad archive."""
    if not archive.is_file():
        fail(f"{archive} is not a file", code=2)


# --------------------------------------------------------------------- integrity


@ingest_app.command("check")
def integrity_check() -> None:
    """Audit the runtime database, its content objects and the configured roots. Read-only."""
    report = _run(lambda: _check())
    table = Table(title="integrity", show_header=True, header_style="bold")
    for column in ("Section", "Result", "Detail"):
        table.add_column(column)
    for section in report.sections:
        style = _SEVERITY_STYLE[section.severity]
        table.add_row(section.name, f"[{style}]{section.severity.value}[/{style}]", section.summary)
    console.print(table)
    for section in report.sections:
        if section.findings:
            console.print(f"[bold]{section.name}[/bold]")
            for finding in section.findings:
                console.print(f"  - {finding}")
    console.print("")
    if report.passed:
        console.print(f"[green]Result: PASS[/green] (worst: {report.worst.value})")
        return
    console.print(f"[red]Result: FAIL[/red] (worst: {report.worst.value})")
    if report.has_critical:
        console.print(
            "A critical finding means stored authority disagrees with itself. Nothing here "
            "repairs it: keep the runtime as it is and decide what to do with it."
        )
    raise typer.Exit(code=1)


async def _check() -> IntegrityReport:
    """Audit the runtime read-only.

    The database is opened read-only, so this command cannot create it, migrate it or change its
    journal mode — and a missing file is reported rather than fabricated.
    """
    from assistant.application.paths import AppPaths
    from assistant.domain.integrity import IntegritySection, IntegritySeverity
    from assistant.store.db import Database

    clock = bootstrap.system_clock()
    database_file = AppPaths.resolve().database_file
    if not database_file.is_file():
        return IntegrityReport(
            sections=(
                IntegritySection(
                    "database",
                    IntegritySeverity.FAIL,
                    f"there is no runtime database at {database_file.name}",
                ),
            )
        )
    database = Database.read_only(database_file)
    service: IntegrityService = bootstrap.integrity_service(clock, database)
    return await service.check()


# ------------------------------------------------------------------------ backup


@backup_app.command("create")
def backup_create(
    target: Annotated[
        Path, typer.Argument(help=f"Where to write the archive (*{ARCHIVE_SUFFIX}).")
    ],
) -> None:
    """Write a consistent backup of the runtime state. An existing file is never overwritten."""
    result = _run(lambda: _create(target))
    console.print(f"[green]Backup written:[/green] {result.path}")
    table = Table(title="backup", show_header=False, title_justify="left")
    table.add_row("Application version", result.manifest.application_version)
    table.add_row("Created", format_local(result.manifest.created_at))
    table.add_row("Database SHA-256", result.database_sha256)
    table.add_row("Mail objects", str(result.mail_object_count))
    table.add_row("Web snapshots", str(result.web_object_count))
    console.print(table)
    console.print("")
    console.print(
        "The archive contains the runtime database and the content objects it references. "
        "It does not contain credentials, the eHall browser profile, application config or "
        "derived knowledge indexes."
    )


async def _create(target: Path) -> BackupResult:
    clock = bootstrap.system_clock()
    service: BackupService = bootstrap.backup_service(clock)
    return service.create(target)


@backup_app.command("verify")
def backup_verify(
    archive: Annotated[Path, typer.Argument(help="The archive to verify.")]
) -> None:
    """Verify an archive read-only: structure, hashes, database pragmas and migrations."""
    _require_archive(archive)
    result = _run(lambda: _verify(archive))
    table = Table(title="backup verify", show_header=False, title_justify="left")
    table.add_row("Archive", str(result.path))
    table.add_row("Manifest format version", str(result.manifest.format_version))
    table.add_row("Application version", result.manifest.application_version)
    table.add_row("Database integrity", "OK" if result.database_integrity_ok else "FAILED")
    table.add_row("Foreign keys", "OK" if result.foreign_keys_ok else "FAILED")
    table.add_row("Migrations", "OK" if result.migrations_match else "MISMATCH")
    console.print(table)
    for problem in result.problems:
        console.print(f"[red]- {problem}[/red]")
    console.print("")
    if result.is_valid:
        console.print("[green]Result: VALID[/green]")
        return
    console.print("[red]Result: INVALID[/red]")
    raise typer.Exit(code=1)


async def _verify(archive: Path) -> VerificationResult:
    clock = bootstrap.system_clock()
    service: BackupService = bootstrap.backup_service(clock)
    return service.verify(archive)


@backup_app.command("inspect")
def backup_inspect(
    archive: Annotated[Path, typer.Argument(help="The archive to inspect.")]
) -> None:
    """Show an archive's manifest without extracting its database. Read-only."""
    _require_archive(archive)
    manifest = _run(lambda: _inspect(archive))
    table = Table(title="backup inspect", show_header=False, title_justify="left")
    table.add_row("Archive", str(archive))
    table.add_row("Format version", str(manifest.format_version))
    table.add_row("Application version", manifest.application_version)
    table.add_row("Created", format_local(manifest.created_at))
    table.add_row("Database SHA-256", manifest.database_sha256)
    table.add_row("Mail objects", str(len(manifest.mail_objects)))
    table.add_row("Web snapshots", str(len(manifest.web_snapshots)))
    table.add_row(
        "Migrations",
        f"{len(manifest.migration_files)} file(s), "
        f"{manifest.migration_files[0]} … {manifest.migration_files[-1]}",
    )
    console.print(table)
    counts = Table(title="counts", show_header=False, title_justify="left")
    for key, value in sorted(manifest.counts.to_document().items()):
        counts.add_row(key.replace("_", " "), str(value))
    console.print(counts)
    console.print("")
    console.print("The manifest carries identity only: no content, no credential, no path.")


async def _inspect(archive: Path) -> BackupManifest:
    clock = bootstrap.system_clock()
    service: BackupService = bootstrap.backup_service(clock)
    return service.inspect(archive)


@backup_app.command("restore")
def backup_restore(
    archive: Annotated[Path, typer.Argument(help="The archive to restore.")],
    to: Annotated[
        Path,
        typer.Option("--to", help="An empty (or absent) directory to restore into."),
    ],
) -> None:
    """Restore into a new staging directory. Never in place, never into the live runtime."""
    _require_archive(archive)
    result = _run(lambda: _restore(archive, to))
    console.print("[green]Restore completed successfully.[/green]")
    table = Table(title="restore", show_header=False, title_justify="left")
    table.add_row("Destination", str(result.destination))
    table.add_row("From backup", str(archive))
    table.add_row("Application version", result.manifest.application_version)
    table.add_row("Approval challenges invalidated", str(result.challenges_invalidated))
    table.add_row("Live approvals superseded", str(result.approvals_superseded))
    table.add_row("Mobile pairing codes invalidated", str(result.pairing_tokens_invalidated))
    table.add_row("Mobile sessions revoked", str(result.sessions_revoked))
    console.print(table)
    console.print("")
    console.print(
        "Historical actions, approvals and executions were preserved, and unresolved executions "
        "stayed unresolved: a restore does not claim an external effect was undone."
    )
    console.print(
        "Credentials were not restored: SMTP and IMAP passwords, the model API key and the eHall "
        "browser profile must be provided again. Re-run `pw mobile pair` for each phone."
    )


async def _restore(archive: Path, destination: Path) -> RestoreResult:
    clock = bootstrap.system_clock()
    service: BackupService = bootstrap.backup_service(clock)
    return service.restore(archive, destination)


def _digest_of(path: Path) -> str:
    """A small helper used by tests and diagnostics: the hash of one file."""
    return sha256_hex(path.read_bytes())


def register(app: typer.Typer) -> None:
    """Register the `pw integrity` and `pw backup` groups on the root app."""
    app.add_typer(ingest_app, name="integrity")
    app.add_typer(backup_app, name="backup")


__all__ = ["backup_app", "ingest_app", "register"]
