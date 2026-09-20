"""RuntimeBackup port: snapshot the authority, and read a snapshot back (ADR-0031).

Two operations, both about the same object — the SQLite runtime database — and both deliberately
narrow:

- **`snapshot_to`** copies the live database into a destination file using SQLite's own consistent
  backup API. A WAL database copied with `shutil.copy` is a torn file; this port exists so that the
  only way to take a backup is the way SQLite supports;
- **`inspect`** reads a *snapshot* (never the live database) and returns what the archive needs:
  integrity and foreign-key results, the applied migrations, the referenced content keys with the
  hashes the snapshot recorded, and the counts that go into the manifest. Reading them from the
  snapshot is what makes a backup a point-in-time object rather than a moving target.

Nothing here exposes a connection, a transaction or SQL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from assistant.domain.backup import BackupCounts, RestoreFinalization


@dataclass(frozen=True, slots=True)
class ReferencedObject:
    """One content object the snapshot references, with the hash the database recorded."""

    storage_key: str
    sha256: str


@dataclass(frozen=True, slots=True)
class SnapshotInspection:
    """What one snapshot says about itself."""

    integrity_problems: tuple[str, ...] = ()
    foreign_key_problems: tuple[str, ...] = ()
    applied_migrations: tuple[str, ...] = ()
    mail_objects: tuple[ReferencedObject, ...] = ()
    web_snapshots: tuple[ReferencedObject, ...] = ()
    counts: BackupCounts = field(default_factory=BackupCounts)

    @property
    def is_consistent(self) -> bool:
        """Whether SQLite itself reports this database as sound."""
        return not self.integrity_problems


class RuntimeBackup(Protocol):
    """Snapshots the runtime database and inspects a snapshot."""

    def snapshot_to(self, destination: Path) -> None:
        """Write a consistent copy of the live runtime database to `destination`.

        Raises:
            StoreError: the snapshot could not be taken.
        """
        ...

    def inspect(self, snapshot: Path) -> SnapshotInspection:
        """Read one snapshot and report its integrity, migrations and referenced objects."""
        ...

    def finalize_restored(
        self, snapshot: Path, *, now: datetime
    ) -> RestoreFinalization:
        """Invalidate every outstanding approval capability in a restored database.

        A restore may not preserve capability: an approval that was outstanding when the backup
        was taken must not still permit anything afterwards, and a phone must pair again.
        """
        ...


__all__ = ["ReferencedObject", "RuntimeBackup", "SnapshotInspection"]
