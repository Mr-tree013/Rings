"""SQLite implementation of the RuntimeBackup port, plus restore finalization (ADR-0031).

Three things live here, and all three are about SQLite rather than about policy:

- **`snapshot_to`** opens the live database and copies it with `sqlite3.Connection.backup`, which is
  the only way to copy a WAL database consistently. Copying the file, or the file plus `-wal` and
  `-shm`, would produce a torn backup that might still open — which is worse than one that fails;
- **`inspect`** reads a snapshot with `PRAGMA integrity_check`, `PRAGMA foreign_key_check`, the
  applied migration list, the content keys the snapshot references and the counts that go into the
  manifest. It never touches the live database, so a backup is a point-in-time object;
- **`finalize_restored`** runs the one write a restore performs on a *restored copy*: every
  outstanding authorization capability is invalidated. Approvals are superseded (they were never
  consumed — saying they were would be a lie), challenges and pairing tokens are consumed, and
  sessions are revoked. Historical rows stay exactly where they are.

Nothing in this module deletes a row, and nothing in it contacts anything.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from assistant.adapters.runtime.permissions import (
    ensure_private_directory,
    ensure_private_file,
)
from assistant.domain.backup import BackupCounts, RestoreFinalization
from assistant.domain.errors import InvalidBackupArchive
from assistant.ports.runtime_backup import ReferencedObject, SnapshotInspection
from assistant.store.errors import StoreError
from assistant.store.serialization import to_utc_iso

_COUNT_QUERIES: tuple[tuple[str, str], ...] = (
    ("tasks", "SELECT count(*) FROM tasks"),
    ("cases", "SELECT count(*) FROM cases"),
    ("actions", "SELECT count(*) FROM action_requests"),
    ("approvals", "SELECT count(*) FROM approvals"),
    ("executions", "SELECT count(*) FROM execution_runs"),
    ("mail_messages", "SELECT count(*) FROM mail_messages"),
    ("mail_drafts", "SELECT count(*) FROM mail_drafts"),
    ("web_observations", "SELECT count(*) FROM web_observations"),
    ("manual_inputs", "SELECT count(*) FROM manual_inputs"),
    ("fact_candidates", "SELECT count(*) FROM fact_candidates"),
    ("confirmed_facts", "SELECT count(*) FROM confirmed_facts"),
    ("playbook_candidates", "SELECT count(*) FROM playbook_candidates"),
    ("playbooks", "SELECT count(*) FROM playbooks"),
    ("inbound_events", "SELECT count(*) FROM inbound_events"),
)


class SqliteRuntimeBackup:
    """Snapshots the runtime database, inspects snapshots, and finalizes a restored copy."""

    def __init__(self, database_path: Path) -> None:
        self._path = Path(database_path)

    @property
    def path(self) -> Path:
        """The live database file this adapter snapshots."""
        return self._path

    def snapshot_to(self, destination: Path) -> None:
        """Copy the live database into `destination` with SQLite's backup API."""
        target = Path(destination)
        if not self._path.is_file():
            # There is nothing to back up, and creating an empty database here would turn a
            # mistyped path into a "successful" backup of nothing.
            raise InvalidBackupArchive(
                f"there is no runtime database at {self._path.name} to back up"
            )
        ensure_private_directory(target.parent)
        if target.exists():
            raise StoreError("the snapshot destination already exists")
        try:
            # `closing`, not `with`: sqlite3's own context manager is a *transaction*, so a bare
            # `with sqlite3.connect(...)` would leave the descriptors open until the garbage
            # collector noticed.
            with (
                closing(sqlite3.connect(str(self._path))) as source,
                closing(sqlite3.connect(str(target))) as copied,
            ):
                source.backup(copied)
        except sqlite3.Error as exc:
            raise StoreError(f"could not snapshot the runtime database: {exc}") from exc
        # The snapshot carries the same personal state as the live database, so it is private too.
        ensure_private_file(target)

    def inspect(self, snapshot: Path) -> SnapshotInspection:
        """Read one snapshot: pragmas, migrations, referenced objects and counts."""
        path = Path(snapshot)
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error as exc:  # pragma: no cover - depends on the filesystem
            raise InvalidBackupArchive(
                "the database member could not be opened as a SQLite database"
            ) from exc
        try:
            connection.row_factory = sqlite3.Row
            return _inspect_open_connection(connection)
        except sqlite3.Error as exc:
            # A file that is not a database, or one damaged past reading, is a verification
            # failure: the caller reports it as an invalid archive rather than a crash.
            raise InvalidBackupArchive(
                "the database member could not be read as a SQLite database"
            ) from exc
        finally:
            connection.close()

    def finalize_restored(
        self, snapshot: Path, *, now: datetime
    ) -> RestoreFinalization:
        """Invalidate every outstanding authorization capability in a restored database.

        One transaction, one direction: capabilities end, history does not change. Approvals are
        superseded rather than consumed, because a recovery is not the user approving anything;
        challenges and pairing codes are consumed because their only meaning is "cannot be used
        twice"; sessions are revoked so a phone must pair again.
        """
        stamp = to_utc_iso(now)
        try:
            connection = sqlite3.connect(str(snapshot))
            try:
                with connection:
                    challenges = connection.execute(
                        "UPDATE approval_challenges SET consumed_at = ? "
                        "WHERE consumed_at IS NULL",
                        (stamp,),
                    ).rowcount
                    approvals = connection.execute(
                        "UPDATE approvals SET superseded_at = ? "
                        "WHERE consumed_at IS NULL AND superseded_at IS NULL",
                        (stamp,),
                    ).rowcount
                    pairings = connection.execute(
                        "UPDATE mobile_pairing_tokens SET consumed_at = ? "
                        "WHERE consumed_at IS NULL",
                        (stamp,),
                    ).rowcount
                    sessions = connection.execute(
                        "UPDATE mobile_sessions SET revoked_at = ? WHERE revoked_at IS NULL",
                        (stamp,),
                    ).rowcount
                # Leave the restored directory with a single database file: once the finalization
                # transaction has committed, switch out of WAL mode so no `-wal`/`-shm` siblings
                # remain. Both pragmas need no open transaction, which is why they sit outside it.
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("PRAGMA journal_mode = DELETE")
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise StoreError(f"could not finalize the restored database: {exc}") from exc
        return RestoreFinalization(
            challenges_invalidated=challenges,
            approvals_superseded=approvals,
            pairing_tokens_invalidated=pairings,
            sessions_revoked=sessions,
        )


def _inspect_open_connection(connection: sqlite3.Connection) -> SnapshotInspection:
    """Read everything the archive needs from an open snapshot connection."""
    integrity = tuple(
        str(row[0])
        for row in connection.execute("PRAGMA integrity_check").fetchall()
        if str(row[0]).lower() != "ok"
    )
    foreign_keys = tuple(
        f"table {row['table']} rowid {row['rowid']} references {row['parent']}"
        for row in connection.execute("PRAGMA foreign_key_check").fetchall()
    )
    migrations = tuple(
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM schema_migrations ORDER BY version"
        ).fetchall()
    )
    mail = tuple(
        ReferencedObject(storage_key=str(row["raw_storage_key"]), sha256=str(row["raw_sha256"]))
        for row in connection.execute(
            "SELECT raw_storage_key, raw_sha256 FROM mail_messages "
            "WHERE raw_storage_key IS NOT NULL AND raw_sha256 IS NOT NULL "
            "ORDER BY raw_storage_key"
        ).fetchall()
    )
    web = tuple(
        ReferencedObject(storage_key=str(row["storage_key"]), sha256=str(row["content_sha256"]))
        for row in connection.execute(
            "SELECT storage_key, content_sha256 FROM web_observations "
            "ORDER BY storage_key, id"
        ).fetchall()
    )
    counts: dict[str, int] = {}
    for name, statement in _COUNT_QUERIES:
        counts[name] = int(connection.execute(statement).fetchone()[0])
    return SnapshotInspection(
        integrity_problems=integrity,
        foreign_key_problems=foreign_keys,
        applied_migrations=migrations,
        mail_objects=_unique(mail),
        web_snapshots=_unique(web),
        counts=BackupCounts(**counts),
    )


def _unique(objects: tuple[ReferencedObject, ...]) -> tuple[ReferencedObject, ...]:
    """One entry per storage key: a content-addressed object may be referenced many times."""
    seen: dict[str, ReferencedObject] = {}
    for item in objects:
        seen.setdefault(item.storage_key, item)
    return tuple(seen[key] for key in sorted(seen))


__all__ = ["RestoreFinalization", "SqliteRuntimeBackup"]
