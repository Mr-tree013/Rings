"""Integration tests for the migration runner, against real SQLite files.

sqlite3 is never mocked here: durability, ordering and rollback are the behaviours under
test, and they only mean something against a real database.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from assistant.store.db import BUSY_TIMEOUT_MS, Database
from assistant.store.errors import MigrationError
from assistant.store.migrations import (
    SCHEMA_MIGRATIONS_TABLE,
    applied_versions,
    apply_migrations,
)
from tests.support.fakes import FakeClock

RAW_INSERT = """
INSERT INTO inbound_events (
    id, source, external_id, event_type, content,
    received_at, status, attempts, last_error, created_at, updated_at
) VALUES (
    :id, :source, :external_id, :event_type, :content,
    :received_at, :status, :attempts, :last_error, :created_at, :updated_at
)
"""

NOW = "2026-09-19T12:00:00.000000+00:00"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = Database.open(tmp_path / "assistant.db")
    yield db
    db.close()


def _write_migration(directory: Path, name: str, sql: str) -> None:
    (directory / name).write_text(sql, encoding="utf-8")


def _table_names(database: Database) -> set[str]:
    rows = database.connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {str(row["name"]) for row in rows}


def _index_sql(database: Database, name: str) -> str:
    row = database.connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)
    ).fetchone()
    assert row is not None, f"index {name} does not exist"
    return " ".join(str(row["sql"]).split())


def _insert_raw(database: Database, **overrides: object) -> None:
    values: dict[str, object] = {
        "id": str(uuid4()),
        "source": "smail",
        "external_id": "42",
        "event_type": "mail.received",
        "content": None,
        "received_at": NOW,
        "status": "RECEIVED",
        "attempts": 0,
        "last_error": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    database.connection.execute(RAW_INSERT, values)


def test_fresh_database_applies_the_initial_migration(
    database: Database, clock: FakeClock
) -> None:
    applied = apply_migrations(database, clock=clock)

    assert [migration.version for migration in applied] == ["0001"]
    assert [migration.name for migration in applied] == ["0001_initial.sql"]


def test_schema_migrations_records_the_applied_version(
    database: Database, clock: FakeClock
) -> None:
    apply_migrations(database, clock=clock)

    row = database.connection.execute(
        f"SELECT version, name, applied_at FROM {SCHEMA_MIGRATIONS_TABLE}"
    ).fetchone()

    assert row is not None
    assert row["version"] == "0001"
    assert row["name"] == "0001_initial.sql"
    assert row["applied_at"] == "2026-09-19T12:00:00.000000+00:00"


def test_running_migrations_twice_is_a_noop(database: Database, clock: FakeClock) -> None:
    apply_migrations(database, clock=clock)

    assert apply_migrations(database, clock=clock) == ()
    assert applied_versions(database) == ("0001",)


def test_required_pragmas_are_effective(database: Database, clock: FakeClock) -> None:
    apply_migrations(database, clock=clock)

    assert database.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert database.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert database.connection.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS


def test_inbound_events_table_exists_with_required_indexes(
    database: Database, clock: FakeClock
) -> None:
    apply_migrations(database, clock=clock)

    assert "inbound_events" in _table_names(database)
    assert SCHEMA_MIGRATIONS_TABLE in _table_names(database)
    unique_index = _index_sql(database, "inbound_events_source_external_id_key")
    assert "CREATE UNIQUE INDEX" in unique_index.upper()
    assert "WHERE external_id IS NOT NULL" in unique_index
    assert "inbound_events_status_received_at_idx" in {
        str(row["name"])
        for row in database.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }


def test_missing_migrations_directory_fails(
    database: Database, clock: FakeClock, tmp_path: Path
) -> None:
    with pytest.raises(MigrationError, match="does not exist"):
        apply_migrations(database, clock=clock, directory=tmp_path / "absent")


def test_unrecognised_migration_filename_fails(
    database: Database, clock: FakeClock, tmp_path: Path
) -> None:
    _write_migration(tmp_path, "oops.sql", "CREATE TABLE t (x);")

    with pytest.raises(MigrationError, match="not recognised"):
        apply_migrations(database, clock=clock, directory=tmp_path)


def test_duplicate_migration_versions_fail(
    database: Database, clock: FakeClock, tmp_path: Path
) -> None:
    _write_migration(tmp_path, "0001_first.sql", "CREATE TABLE a (x);")
    _write_migration(tmp_path, "0001_second.sql", "CREATE TABLE b (x);")

    with pytest.raises(MigrationError, match="duplicate migration version 0001"):
        apply_migrations(database, clock=clock, directory=tmp_path)


def test_out_of_order_migration_fails(
    database: Database, clock: FakeClock, tmp_path: Path
) -> None:
    _write_migration(tmp_path, "0001_first.sql", "CREATE TABLE a (x);")
    apply_migrations(database, clock=clock, directory=tmp_path)
    _write_migration(tmp_path, "0000_late_arrival.sql", "CREATE TABLE b (x);")

    with pytest.raises(MigrationError, match="out of order"):
        apply_migrations(database, clock=clock, directory=tmp_path)


def test_migrations_apply_in_version_order(
    database: Database, clock: FakeClock, tmp_path: Path
) -> None:
    _write_migration(tmp_path, "0002_second.sql", "CREATE TABLE second (x);")
    _write_migration(tmp_path, "0001_first.sql", "CREATE TABLE first (x);")

    applied = apply_migrations(database, clock=clock, directory=tmp_path)

    assert [migration.version for migration in applied] == ["0001", "0002"]
    assert {"first", "second"} <= _table_names(database)


def test_failing_migration_is_rolled_back(
    database: Database, clock: FakeClock, tmp_path: Path
) -> None:
    _write_migration(
        tmp_path,
        "0001_broken.sql",
        "CREATE TABLE half_written (x);\nSELECT * FROM table_that_does_not_exist;",
    )

    with pytest.raises(MigrationError, match=r"0001_broken\.sql"):
        apply_migrations(database, clock=clock, directory=tmp_path)

    assert "half_written" not in _table_names(database)
    assert applied_versions(database) == ()


def test_empty_migration_fails(database: Database, clock: FakeClock, tmp_path: Path) -> None:
    _write_migration(tmp_path, "0001_empty.sql", "   \n")

    with pytest.raises(MigrationError, match="is empty"):
        apply_migrations(database, clock=clock, directory=tmp_path)


def test_migration_must_not_manage_its_own_transaction(
    database: Database, clock: FakeClock, tmp_path: Path
) -> None:
    _write_migration(
        tmp_path,
        "0001_manual_transaction.sql",
        "BEGIN TRANSACTION;\nCREATE TABLE t (x);\nCOMMIT;",
    )

    with pytest.raises(MigrationError, match="must not manage transactions"):
        apply_migrations(database, clock=clock, directory=tmp_path)


def test_database_rejects_a_duplicate_identity_even_without_the_repository(
    database: Database, clock: FakeClock
) -> None:
    apply_migrations(database, clock=clock)
    _insert_raw(database)

    with pytest.raises(sqlite3.IntegrityError):
        _insert_raw(database)


def test_database_allows_many_rows_without_external_id(
    database: Database, clock: FakeClock
) -> None:
    apply_migrations(database, clock=clock)

    _insert_raw(database, source="cli", external_id=None)
    _insert_raw(database, source="cli", external_id=None)

    count = database.connection.execute("SELECT count(*) FROM inbound_events").fetchone()[0]
    assert count == 2


def test_database_constrains_status_attempts_and_blank_fields(
    database: Database, clock: FakeClock
) -> None:
    apply_migrations(database, clock=clock)

    with pytest.raises(sqlite3.IntegrityError, match="status_is_known"):
        _insert_raw(database, status="WEIRD", external_id="status-check")
    with pytest.raises(sqlite3.IntegrityError, match="attempts_not_negative"):
        _insert_raw(database, attempts=-1, external_id="attempts-check")
    with pytest.raises(sqlite3.IntegrityError, match="failed_carries_error"):
        _insert_raw(database, status="FAILED", external_id="failed-check")
    with pytest.raises(sqlite3.IntegrityError, match="source_not_blank"):
        _insert_raw(database, source="   ", external_id="source-check")
