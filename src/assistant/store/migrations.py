"""Forward-only migration runner for the runtime SQLite store (ADR-0003, ADR-0008).

The runner is intentionally synchronous: the daemon applies migrations during startup,
before any long-running asyncio service exists, so there is no event loop to protect
(ADR-0009). It opens its own connection per step and never runs inside a worker thread.

Why the transaction control sits inside the SQL text: Python's `executescript()`
performs an implicit COMMIT before it runs, so an outer `BEGIN` cannot make a migration
atomic (verified against CPython 3.13). The runner therefore builds
`BEGIN IMMEDIATE; <migration>; INSERT INTO schema_migrations ...; COMMIT;` and hands the
whole thing to `executescript`, keeping the schema change and its bookkeeping in a single
transaction.

There is no downgrade: forward migrations only, and re-running `apply_migrations` is a
no-op.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from assistant.ports.clock import Clock
from assistant.store.db import Database
from assistant.store.errors import DatabaseMigrationIncompatible, MigrationError
from assistant.store.serialization import to_utc_iso

SCHEMA_MIGRATIONS_TABLE = "schema_migrations"

MIGRATION_FILENAME_PATTERN = re.compile(r"^(?P<version>\d{4})_(?P<name>[A-Za-z0-9_.-]+)\.sql$")

_MANAGED_TRANSACTION = re.compile(
    r"(?im)^\s*(?:BEGIN\s+(?:DEFERRED|IMMEDIATE|EXCLUSIVE|TRANSACTION)|COMMIT|ROLLBACK)\b"
)

_BOOKKEEPING_SQL = (
    "BEGIN IMMEDIATE;\n"
    f"CREATE TABLE IF NOT EXISTS {SCHEMA_MIGRATIONS_TABLE} (\n"
    "    version    TEXT PRIMARY KEY,\n"
    "    name       TEXT NOT NULL,\n"
    "    applied_at TEXT NOT NULL\n"
    ");\n"
    "COMMIT;\n"
)


@dataclass(frozen=True, slots=True)
class MigrationFile:
    """One migration file on disk."""

    version: str
    name: str
    path: Path


def default_migrations_dir() -> Path:
    """Where this build's migration files are.

    An installed wheel carries them inside the package (`assistant/migrations/`, put there by the
    build), which is what makes an installed `pw` or `assistantd` able to migrate a runtime at all
    (ADR-0032). Running from a checkout they are still the repository's root `migrations/`
    directory, so the two layouts both work and neither depends on the other.
    """
    packaged = Path(__file__).resolve().parents[1] / "migrations"
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[3] / "migrations"


def discover_migrations(directory: Path) -> tuple[MigrationFile, ...]:
    """Return the migration files in ascending version order.

    Raises:
        MigrationError: the directory is missing, a filename is unrecognised, or two
            files claim the same version.
    """
    if not directory.is_dir():
        raise MigrationError(f"migrations directory does not exist: {directory}")
    by_version: dict[str, MigrationFile] = {}
    for path in sorted(directory.glob("*.sql")):
        match = MIGRATION_FILENAME_PATTERN.match(path.name)
        if match is None:
            raise MigrationError(
                f"migration filename is not recognised (expected NNNN_name.sql): {path.name}"
            )
        version = match.group("version")
        if version in by_version:
            raise MigrationError(
                f"duplicate migration version {version}: "
                f"{by_version[version].name} and {path.name}"
            )
        by_version[version] = MigrationFile(version=version, name=path.name, path=path)
    return tuple(by_version[version] for version in sorted(by_version))


def applied_versions(database: Database) -> tuple[str, ...]:
    """Return the versions already recorded in `schema_migrations`."""
    return tuple(sorted(_load_applied(database)))


def require_compatible_history(
    database: Database,
    *,
    directory: Path | None = None,
) -> None:
    """Refuse a database whose applied history this binary does not ship (ADR-0032).

    Two things are refused, and both of them are about the *database* being from somewhere else:

    * an applied version this binary does not have — the database was written by a newer build, and
      running today's SQL against it would corrupt data rather than fail;
    * an applied version whose recorded *name* differs from the shipped file — the history was
      rewritten, and this project's migrations are immutable.

    Unapplied shipped migrations are not an error here: a host is allowed to be one upgrade behind,
    and `apply_migrations` applies them forward. Databases this binary has never migrated at all (no
    `schema_migrations` table yet) are also fine — there is no history to disagree with.

    Raises:
        DatabaseMigrationIncompatible: the database's history is newer or rewritten.
    """
    migrations_dir = directory if directory is not None else default_migrations_dir()
    shipped = {item.version: item.name for item in discover_migrations(migrations_dir)}
    if not _has_bookkeeping_table(database):
        return
    applied = _load_applied(database)
    unknown = sorted(
        version for version, name in applied.items() if shipped.get(version) != name
    )
    if not unknown:
        return
    listed = ", ".join(unknown[:5])
    more = "" if len(unknown) <= 5 else f" (+{len(unknown) - 5} more)"
    raise DatabaseMigrationIncompatible(
        "this database was written by a newer or rewritten migration history "
        f"(unknown to this build: {listed}{more}); upgrade the software or use the binary that "
        "wrote this runtime. Downgrading a database is not supported."
    )


def apply_migrations(
    database: Database,
    *,
    clock: Clock,
    directory: Path | None = None,
) -> tuple[MigrationFile, ...]:
    """Apply every pending migration and return the ones that were applied now."""
    migrations_dir = directory if directory is not None else default_migrations_dir()
    migrations = discover_migrations(migrations_dir)
    _ensure_bookkeeping_table(database)
    applied = _load_applied(database)
    highest_applied = max(applied) if applied else None
    applied_now: list[MigrationFile] = []
    for migration in migrations:
        if migration.version in applied:
            continue
        if highest_applied is not None and migration.version < highest_applied:
            raise MigrationError(
                f"migration {migration.name} would be applied out of order: "
                f"version {highest_applied} is already applied"
            )
        _apply_one(database, migration, clock)
        applied[migration.version] = migration.name
        highest_applied = migration.version
        applied_now.append(migration)
    return tuple(applied_now)


def _ensure_bookkeeping_table(database: Database) -> None:
    try:
        with database.connect() as connection:
            connection.executescript(_BOOKKEEPING_SQL)
    except sqlite3.Error as exc:
        raise MigrationError(f"could not create {SCHEMA_MIGRATIONS_TABLE}: {exc}") from exc


def _has_bookkeeping_table(database: Database) -> bool:
    """Whether this database has been migrated at all. A missing table is not a mismatch."""
    with database.connect() as connection:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (SCHEMA_MIGRATIONS_TABLE,),
        ).fetchone()
    return row is not None


def _load_applied(database: Database) -> dict[str, str]:
    try:
        with database.connect() as connection:
            rows = connection.execute(
                f"SELECT version, name FROM {SCHEMA_MIGRATIONS_TABLE}"
            ).fetchall()
    except sqlite3.Error as exc:
        raise MigrationError(f"could not read {SCHEMA_MIGRATIONS_TABLE}: {exc}") from exc
    return {str(row["version"]): str(row["name"]) for row in rows}


def _apply_one(database: Database, migration: MigrationFile, clock: Clock) -> None:
    sql = migration.path.read_text(encoding="utf-8")
    if not sql.strip():
        raise MigrationError(f"migration {migration.name} is empty")
    if _MANAGED_TRANSACTION.search(sql):
        raise MigrationError(f"migration {migration.name} must not manage transactions itself")
    applied_at = to_utc_iso(clock.now())
    script = "\n".join(
        (
            "BEGIN IMMEDIATE;",
            sql,
            "INSERT INTO "
            f"{SCHEMA_MIGRATIONS_TABLE} (version, name, applied_at) VALUES ("
            f"{_sql_literal(migration.version)}, {_sql_literal(migration.name)}, "
            f"{_sql_literal(applied_at)});",
            "COMMIT;",
        )
    )
    with database.connect() as connection:
        try:
            connection.executescript(script)
        except sqlite3.Error as exc:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise MigrationError(f"migration {migration.name} failed: {exc}") from exc


def _sql_literal(value: str) -> str:
    if "\x00" in value:
        raise MigrationError(f"value may not contain NUL bytes: {value!r}")
    return "'" + value.replace("'", "''") + "'"


__all__ = [
    "MIGRATION_FILENAME_PATTERN",
    "SCHEMA_MIGRATIONS_TABLE",
    "MigrationFile",
    "applied_versions",
    "apply_migrations",
    "default_migrations_dir",
    "discover_migrations",
    "require_compatible_history",
]
