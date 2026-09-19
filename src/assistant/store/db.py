"""SQLite connections: pragmas and explicit transactions (ADR-0003, ADR-0008, ADR-0009).

`sqlite3.Connection` objects never cross a thread boundary: every connection is created,
used and closed inside the thread that runs the SQL. `Database` is therefore a connection
*factory plus pragma policy*, not a connection owner. Application code reaches storage
through the async repository, which runs each blocking operation in a worker thread
(`asyncio.to_thread`, ADR-0009).

Design notes:

- Connections are never module-level globals and never cached between operations.
- `isolation_level=None` disables sqlite3's implicit transaction management, so
  transactions are always explicit (`BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`).
- `PRAGMA journal_mode = WAL` is required for file databases. In-memory databases cannot
  use WAL (SQLite reports `memory`), so the requirement is enforced for real files only
  instead of pretending otherwise in tests. A shared in-memory database is not usable
  with per-operation connections, so tests use real files.
- The sqlite3 thread guard is deliberately left enabled: no `check_same_thread` argument
  is ever passed to `connect`, because disabling the guard would hide cross-thread use of
  a connection instead of preventing it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from assistant.store.errors import DatabaseConfigurationError

MEMORY_PATH = ":memory:"
JOURNAL_MODE_WAL = "wal"
JOURNAL_MODE_DELETE = "delete"
DEFAULT_JOURNAL_MODE = JOURNAL_MODE_WAL
REQUIRED_JOURNAL_MODE = JOURNAL_MODE_WAL
"""The runtime store's journal mode. Derived stores (knowledge indexes) may choose another."""
BUSY_TIMEOUT_MS = 5000


class Database:
    """Connection factory and pragma policy for one SQLite database file."""

    def __init__(
        self,
        path: str,
        *,
        create_parent: bool = True,
        journal_mode: str = DEFAULT_JOURNAL_MODE,
    ) -> None:
        self._path = path
        self._create_parent = create_parent
        self._journal_mode = journal_mode

    @classmethod
    def at(
        cls,
        path: str | Path,
        *,
        create_parent: bool = True,
        journal_mode: str = DEFAULT_JOURNAL_MODE,
    ) -> Database:
        """Describe the database at `path`. Nothing is opened or created here.

        Opening the file happens later, in `connect()`, inside whichever thread runs the
        SQL — which is what makes a `Database` safe to hand to a worker thread.
        """
        return cls(str(path), create_parent=create_parent, journal_mode=journal_mode)

    @property
    def path(self) -> str:
        """The path this database was declared with."""
        return self._path

    @property
    def is_file_database(self) -> bool:
        """Whether this database is backed by a file on disk."""
        return self._path != MEMORY_PATH

    @property
    def journal_mode(self) -> str:
        """The journal mode required of every connection to this database."""
        return self._journal_mode

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Open a configured connection in the calling thread, then close it.

        When `create_parent` is set, the parent directory is created first so a first-run
        daemon can write its runtime database.
        """
        target = self._path
        if self.is_file_database and self._create_parent:
            resolved = Path(target).expanduser()
            resolved.parent.mkdir(parents=True, exist_ok=True)
            target = str(resolved)
        connection = sqlite3.connect(target, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            _configure(
                connection,
                path=target,
                is_file_database=self.is_file_database,
                journal_mode=self._journal_mode,
            )
            yield connection
        finally:
            connection.close()


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block inside one `BEGIN IMMEDIATE` transaction on `connection`.

    Commits on success, rolls back on any exception (including cancellation). Nested
    transactions are refused rather than silently flattened.
    """
    if connection.in_transaction:
        raise DatabaseConfigurationError("nested transactions are not supported")
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    else:
        if connection.in_transaction:
            connection.execute("COMMIT")


def _configure(
    connection: sqlite3.Connection,
    *,
    path: str,
    is_file_database: bool,
    journal_mode: str,
) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    achieved_mode = str(
        connection.execute(f"PRAGMA journal_mode = {journal_mode}").fetchone()[0]
    )
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    if is_file_database and achieved_mode.lower() != journal_mode.lower():
        raise DatabaseConfigurationError(
            f"{path} reports journal_mode={achieved_mode!r}, expected {journal_mode!r}"
        )
    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        raise DatabaseConfigurationError("foreign key enforcement could not be enabled")
    if int(connection.execute("PRAGMA busy_timeout").fetchone()[0]) != BUSY_TIMEOUT_MS:
        raise DatabaseConfigurationError(f"busy_timeout is not {BUSY_TIMEOUT_MS}ms")


__all__ = [
    "BUSY_TIMEOUT_MS",
    "DEFAULT_JOURNAL_MODE",
    "JOURNAL_MODE_DELETE",
    "JOURNAL_MODE_WAL",
    "MEMORY_PATH",
    "REQUIRED_JOURNAL_MODE",
    "Database",
    "transaction",
]
