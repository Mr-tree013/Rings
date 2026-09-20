"""Operational backup, verification and staging restore (ADR-0031).

```text
pw backup create FILE      snapshot the authority → verify referenced objects → write atomically
pw backup verify FILE      read-only: structure, manifest, hashes, database pragmas
pw backup inspect FILE     read-only: the manifest only, no extraction
pw backup restore FILE --to DIR
                           verify → extract into a sibling temp dir → finalize capabilities
                           → validate the result → atomic rename into place
```

Four rules shape every operation here:

- **the snapshot is the reference point.** Referenced objects are read from the *snapshot*, and it
  is the snapshot that decides what belongs in the archive. Objects that arrive in the live runtime
  while the backup runs are simply not part of it;
- **a backup is all-or-nothing.** A missing or mismatched object fails the whole operation instead
  of producing an archive with a hole in it, and nothing appears at the destination until the
  temporary file has been verified;
- **verification is read-only and offline.** It opens the archive, checks names, sizes and hashes,
  extracts the database to a private temporary directory and asks SQLite about it. It contacts no
  mail server, no eHall page, no watcher and no model, and it never repairs anything;
- **restore stages and then renames.** The destination must be empty or absent, it may not be the
  active runtime directory or anything containing or contained by it, and the finished tree appears
  atomically only after the restored database has been finalized and checked.

What a restore explicitly does **not** do is restore capability: outstanding challenges, approvals,
pairing codes and mobile sessions are invalidated, `RUNNING` and `UNKNOWN` executions stay exactly
as unresolved as they were, and nothing claims an external effect was undone.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from assistant.domain.backup import (
    DATABASE_FILENAME,
    DATABASE_MEMBER,
    MAIL_MEMBER_PREFIX,
    MAIL_ROOT_DIRECTORY,
    BackupManifest,
    BackupObject,
    sha256_hex,
)
from assistant.domain.errors import (
    BackupSourceCorrupt,
    BackupSourceMissing,
    InvalidBackupArchive,
    RestoreDestinationRejected,
)
from assistant.ports.backup_archive import ArchiveHandle, BackupArchive
from assistant.ports.clock import Clock
from assistant.ports.runtime_backup import ReferencedObject, RuntimeBackup

LOGGER = logging.getLogger("assistant.backup")

_PACKAGED_MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
_CHECKOUT_MIGRATIONS = Path(__file__).resolve().parents[3] / "migrations"
MIGRATION_DIRECTORY = (
    _PACKAGED_MIGRATIONS if _PACKAGED_MIGRATIONS.is_dir() else _CHECKOUT_MIGRATIONS
)
"""Where this build's reviewed migration files live: inside the installed package, or the
repository's `migrations/` directory when running from a checkout (ADR-0032)."""


def member_path(runtime_root: Path, storage_key: str) -> Path:
    """Where one content object lives inside a runtime directory.

    The two stores were built at different times and their roots differ — raw mail sits under
    `<runtime>/mail/` while web snapshots sit directly under `<runtime>/` — so the mapping is one
    function rather than two call sites that could drift apart.
    """
    if storage_key.startswith(MAIL_MEMBER_PREFIX):
        return runtime_root / MAIL_ROOT_DIRECTORY / storage_key
    return runtime_root / storage_key


@dataclass(frozen=True, slots=True)
class BackupResult:
    """What one backup produced, without a byte of anybody's content."""

    path: Path
    database_sha256: str
    mail_object_count: int
    web_object_count: int
    manifest: BackupManifest


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """What one verification found."""

    path: Path
    manifest: BackupManifest
    database_integrity_ok: bool
    foreign_keys_ok: bool
    migrations_match: bool
    problems: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_valid(self) -> bool:
        """Whether the archive may be trusted as a restore point."""
        return (
            not self.problems
            and self.database_integrity_ok
            and self.foreign_keys_ok
            and self.migrations_match
        )


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """What one restore produced."""

    destination: Path
    manifest: BackupManifest
    challenges_invalidated: int
    approvals_superseded: int
    pairing_tokens_invalidated: int
    sessions_revoked: int
    integrity_ok: bool


class BackupService:
    """Creates, verifies and restores operational backups of the runtime state."""

    def __init__(
        self,
        runtime: Path,
        backup: RuntimeBackup,
        clock: Clock,
        archive: BackupArchive,
        *,
        application_version: str,
        migrations_directory: Path | None = None,
    ) -> None:
        self._runtime = Path(runtime)
        self._backup = backup
        self._archive = archive
        self._clock = clock
        self._version = application_version
        self._migrations = (
            migrations_directory if migrations_directory is not None else MIGRATION_DIRECTORY
        )

    @property
    def runtime(self) -> Path:
        """The runtime directory this service backs up."""
        return self._runtime

    # ------------------------------------------------------------------ create

    def create(self, destination: Path) -> BackupResult:
        """Snapshot, verify every referenced object and write one archive.

        Raises:
            InvalidBackupArchive: the destination exists, or the source database is not sound.
            BackupSourceMissing: the database references an object that is not on disk.
            BackupSourceCorrupt: an object does not match the hash the database recorded.
        """
        target = Path(destination)
        if target.exists():
            raise InvalidBackupArchive(
                f"{target} already exists; this project never overwrites a backup"
            )
        with tempfile.TemporaryDirectory(prefix="growing-assistant-backup-") as staging:
            snapshot = Path(staging) / DATABASE_MEMBER
            self._backup.snapshot_to(snapshot)
            inspection = self._backup.inspect(snapshot)
            if inspection.integrity_problems:
                raise InvalidBackupArchive(
                    "refusing to back up a database that fails its own integrity check"
                )
            if inspection.foreign_key_problems:
                raise InvalidBackupArchive(
                    "refusing to back up a database with foreign key problems"
                )
            mail = self._collect(inspection.mail_objects)
            web = self._collect(inspection.web_snapshots)
            manifest = BackupManifest(
                application_version=self._version,
                created_at=self._clock.now(),
                database_sha256=sha256_hex(snapshot.read_bytes()),
                migration_files=inspection.applied_migrations,
                mail_objects=mail,
                web_snapshots=web,
                counts=inspection.counts,
            )
            objects = tuple(
                (item.storage_key, member_path(self._runtime, item.storage_key))
                for item in (*mail, *web)
            )
            self._archive.write(
                target, manifest=manifest, database=snapshot, objects=objects
            )
        LOGGER.info("backup created mail=%d web=%d", len(mail), len(web))
        return BackupResult(
            path=target,
            database_sha256=manifest.database_sha256,
            mail_object_count=len(mail),
            web_object_count=len(web),
            manifest=manifest,
        )

    # ------------------------------------------------------------------ verify

    def verify(self, archive_path: Path) -> VerificationResult:
        """Check one archive read-only: structure, hashes and database soundness."""
        handle = self._archive.read(Path(archive_path))
        problems: list[str] = []
        with tempfile.TemporaryDirectory(prefix="growing-assistant-verify-") as work:
            work_path = Path(work)
            database = work_path / DATABASE_MEMBER
            members = (
                *handle.manifest.mail_keys(),
                *handle.manifest.web_keys(),
            )
            try:
                handle.extract(DATABASE_MEMBER, database)
                for name in members:
                    target = work_path / "members" / name
                    handle.extract(name, target)
                    target.unlink()
            except InvalidBackupArchive as exc:
                problems.append(str(exc))
            inspection = None
            if database.is_file():
                try:
                    inspection = self._backup.inspect(database)
                except InvalidBackupArchive as exc:
                    # A file whose hash matches the manifest but which is not a database is a
                    # verification failure, not a crash: the operator gets INVALID.
                    problems.append(str(exc))
        expected = self._reviewed_migrations()
        applied = () if inspection is None else inspection.applied_migrations
        return VerificationResult(
            path=Path(archive_path),
            manifest=handle.manifest,
            database_integrity_ok=inspection is not None and inspection.is_consistent,
            foreign_keys_ok=inspection is not None and not inspection.foreign_key_problems,
            migrations_match=inspection is not None and tuple(applied) == expected,
            problems=tuple(problems),
        )

    # ----------------------------------------------------------------- inspect

    def inspect(self, archive_path: Path) -> BackupManifest:
        """Read one archive's manifest without extracting its database."""
        return self._archive.read(Path(archive_path)).manifest

    # ----------------------------------------------------------------- restore

    def restore(self, archive_path: Path, destination: Path) -> RestoreResult:
        """Verify, extract, finalize capabilities and move the result into place.

        Raises:
            RestoreDestinationRejected: the destination is not empty, or is the active runtime.
            InvalidBackupArchive: the archive is unsafe, malformed or fails its verification.
        """
        target = Path(destination).resolve()
        verification = self.verify(archive_path)
        if not verification.is_valid:
            raise InvalidBackupArchive(
                "the archive did not verify: "
                + "; ".join(verification.problems or ("database or migrations failed",))
            )
        # The archive is sound before anything is created: a refused or invalid invocation leaves
        # no directory behind, not even an empty parent.
        self._require_usable_destination(target)
        handle = self._archive.read(Path(archive_path))
        staging = Path(
            tempfile.mkdtemp(prefix=f".{target.name}.restore-", dir=str(target.parent))
        )
        try:
            self._extract_into(handle, staging)
            restored = staging / DATABASE_FILENAME
            finalization = self._backup.finalize_restored(
                restored, now=self._clock.now()
            )
            inspection = self._backup.inspect(restored)
            if inspection.integrity_problems or inspection.foreign_key_problems:
                raise InvalidBackupArchive(
                    "the restored database did not pass its integrity check"
                )
            os.replace(staging, target)
        except BaseException:
            # Only the directory this call created is removed, and only after checking that it is
            # still the staging directory we made.
            if staging.is_dir() and staging.name.startswith(f".{target.name}.restore-"):
                shutil.rmtree(staging, ignore_errors=True)
            raise
        LOGGER.info(
            "restore completed challenges=%d approvals=%d pairings=%d sessions=%d",
            finalization.challenges_invalidated,
            finalization.approvals_superseded,
            finalization.pairing_tokens_invalidated,
            finalization.sessions_revoked,
        )
        return RestoreResult(
            destination=target,
            manifest=handle.manifest,
            challenges_invalidated=finalization.challenges_invalidated,
            approvals_superseded=finalization.approvals_superseded,
            pairing_tokens_invalidated=finalization.pairing_tokens_invalidated,
            sessions_revoked=finalization.sessions_revoked,
            integrity_ok=True,
        )

    # ---------------------------------------------------------------- internals

    def _extract_into(self, handle: ArchiveHandle, staging: Path) -> None:
        """Write the restored runtime tree, member by member."""
        handle.extract(DATABASE_MEMBER, staging / DATABASE_FILENAME)
        for item in (*handle.manifest.mail_objects, *handle.manifest.web_snapshots):
            handle.extract(item.storage_key, member_path(staging, item.storage_key))

    def _require_usable_destination(self, target: Path) -> None:
        """The destination must be empty (or absent) and may not be the live runtime tree."""
        runtime = self._runtime.resolve()
        if target == runtime or runtime in target.parents or target in runtime.parents:
            raise RestoreDestinationRejected(
                target, "it is the active runtime directory, or contains it"
            )
        if target.exists():
            if not target.is_dir():
                raise RestoreDestinationRejected(target, "it exists and is not a directory")
            if any(target.iterdir()):
                raise RestoreDestinationRejected(target, "it exists and is not empty")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)

    def _collect(
        self, objects: tuple[ReferencedObject, ...]
    ) -> tuple[BackupObject, ...]:
        """Verify each referenced object exists inside the runtime and matches its recorded hash."""
        collected: list[BackupObject] = []
        base = self._runtime.resolve()
        for item in objects:
            path = member_path(self._runtime, item.storage_key).resolve()
            if base != path and base not in path.parents:
                raise InvalidBackupArchive(
                    f"the storage key {item.storage_key!r} escapes the runtime directory"
                )
            if not path.is_file():
                raise BackupSourceMissing(item.storage_key)
            payload = path.read_bytes()
            if sha256_hex(payload) != item.sha256:
                raise BackupSourceCorrupt(item.storage_key)
            collected.append(
                BackupObject(
                    storage_key=item.storage_key,
                    sha256=item.sha256,
                    size_bytes=len(payload),
                )
            )
        return tuple(collected)

    def _reviewed_migrations(self) -> tuple[str, ...]:
        return tuple(sorted(path.name for path in self._migrations.glob("*.sql")))


__all__ = [
    "MIGRATION_DIRECTORY",
    "BackupResult",
    "BackupService",
    "RestoreResult",
    "VerificationResult",
    "member_path",
]
