"""SQLite connection handling: pragmas, transactions and lifecycle (ADR-0003, ADR-0008).

Design notes:

- Connections are never module-level globals. Every caller owns the connection it opened.
- `isolation_level=None` disables sqlite3's implicit transaction management so that
  transactions are always explicit (`BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`).
- `PRAGMA journal_mode = WAL` is required for file databases. In-memory databases cannot
  use WAL (SQLite reports `memory`), so the requirement is only enforced for real files
  instead of pretending otherwise in tests.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from assistant.store.errors import DatabaseConfigurationError

MEMORY_PATH = ":memory:"
REQUIRED_JOURNAL_MODE = "wal"
BUSY_TIMEOUT_MS = 5000


class Database:
    """A thin, explicit wrapper around a single `sqlite3` connection."""

    def __init__(self, connection: sqlite3.Connection, path: str) -> None:
        self._connection = connection
        self._path = path

    @classmethod
    def open(cls, path: str | Path, *, create_parent: bool = True) -> Database:
        """Open (and, for files, create) a database and apply the required pragmas."""
        target = str(path)
        if target != MEMORY_PATH and create_parent:
            resolved = Path(target).expanduser()
            resolved.parent.mkdir(parents=True, exist_ok=True)
            target = str(resolved)
        connection = sqlite3.connect(target, isolation_level=None)
        connection.row_factory = sqlite3.Row
        database = cls(connection, target)
        database._configure()
        return database

    @property
    def connection(self) -> sqlite3.Connection:
        """The underlying connection, for store modules in this package."""
        return self._connection

    @property
    def path(self) -> str:
        """The path this database was opened with."""
        return self._path

    @property
    def is_file_database(self) -> bool:
        """Whether this database is backed by a file on disk."""
        return self._path != MEMORY_PATH

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block inside one `BEGIN IMMEDIATE` transaction.

        Commits on success, rolls back on any exception (including cancellation).
        Nested transactions are refused rather than silently flattened.
        """
        if self._connection.in_transaction:
            raise DatabaseConfigurationError("nested transactions are not supported")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield self._connection
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise
        else:
            if self._connection.in_transaction:
                self._connection.execute("COMMIT")

    def close(self) -> None:
        """Close the connection."""
        self._connection.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _configure(self) -> None:
        connection = self._connection
        connection.execute("PRAGMA foreign_keys = ON")
        journal_mode = str(
            connection.execute(f"PRAGMA journal_mode = {REQUIRED_JOURNAL_MODE}").fetchone()[0]
        )
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        if self.is_file_database and journal_mode.lower() != REQUIRED_JOURNAL_MODE:
            raise DatabaseConfigurationError(
                f"{self._path} reports journal_mode={journal_mode!r}, "
                f"expected {REQUIRED_JOURNAL_MODE!r}"
            )
        if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            raise DatabaseConfigurationError("foreign key enforcement could not be enabled")
        if int(connection.execute("PRAGMA busy_timeout").fetchone()[0]) != BUSY_TIMEOUT_MS:
            raise DatabaseConfigurationError(f"busy_timeout is not {BUSY_TIMEOUT_MS}ms")


__all__ = ["BUSY_TIMEOUT_MS", "MEMORY_PATH", "REQUIRED_JOURNAL_MODE", "Database"]

