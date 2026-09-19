"""Integration tests for the migration runner, against real SQLite files.

sqlite3 is never mocked here: durability, ordering and rollback are the behaviours under
test, and they only mean something against a real database. The runner stays synchronous
(ADR-0009); only the repository has an async boundary.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from assistant.store.db import BUSY_TIMEOUT_MS, Database
from assistant.store.errors import MigrationError
from assistant.store.migrations import (
    SCHEMA_MIGRATIONS_TABLE,
    applied_versions,
    apply_migrations,
    default_migrations_dir,
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
def database(tmp_path: Path) -> Database:
    return Database.at(tmp_path / "assistant.db")


def _write_migration(directory: Path, name: str, sql: str) -> None:
    (directory / name).write_text(sql, encoding="utf-8")


def _table_names(database: Database) -> set[str]:
    with database.connect() as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row["name"]) for row in rows}


def _index_names(database: Database) -> set[str]:
    with database.connect() as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
    return {str(row["name"]) for row in rows}


def _index_sql(database: Database, name: str) -> str:
    with database.connect() as connection:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)
        ).fetchone()
    assert row is not None, f"index {name} does not exist"
    return " ".join(str(row["sql"]).split())


def _row_count(database: Database) -> int:
    with database.connect() as connection:
        return int(connection.execute("SELECT count(*) FROM inbound_events").fetchone()[0])


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
    with database.connect() as connection:
        connection.execute(RAW_INSERT, values)


def test_fresh_database_applies_the_initial_migration(database: Database, clock: FakeClock) -> None:
    applied = apply_migrations(database, clock=clock)

    assert [migration.version for migration in applied] == [
        "0001", "0002", "0003", "0004", "0005",
    ]
    assert [migration.name for migration in applied] == [
        "0001_initial.sql",
        "0002_event_processing_leases.sql",
        "0003_storage_catalog.sql",
        "0004_commitment_core.sql",
        "0005_planning_proposals.sql",
    ]


def test_schema_migrations_records_the_applied_version(
    database: Database, clock: FakeClock
) -> None:
    apply_migrations(database, clock=clock)

    with database.connect() as connection:
        row = connection.execute(
            f"SELECT version, name, applied_at FROM {SCHEMA_MIGRATIONS_TABLE}"
        ).fetchone()

    assert row is not None
    assert row["version"] == "0001"
    assert row["name"] == "0001_initial.sql"
    assert row["applied_at"] == "2026-09-19T12:00:00.000000+00:00"


def test_running_migrations_twice_is_a_noop(database: Database, clock: FakeClock) -> None:
    apply_migrations(database, clock=clock)

    assert apply_migrations(database, clock=clock) == ()
    assert applied_versions(database) == ("0001", "0002", "0003", "0004", "0005")


def test_required_pragmas_are_effective(database: Database, clock: FakeClock) -> None:
    apply_migrations(database, clock=clock)

    with database.connect() as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]

    assert journal_mode == "wal"
    assert foreign_keys == 1
    assert busy_timeout == BUSY_TIMEOUT_MS


def test_every_connection_is_configured_the_same_way(
    database: Database, clock: FakeClock
) -> None:
    apply_migrations(database, clock=clock)

    with database.connect() as first, database.connect() as second:
        assert first is not second
        assert first.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert second.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert second.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS


def test_inbound_events_table_exists_with_required_indexes(
    database: Database, clock: FakeClock
) -> None:
    apply_migrations(database, clock=clock)

    assert "inbound_events" in _table_names(database)
    assert SCHEMA_MIGRATIONS_TABLE in _table_names(database)
    unique_index = _index_sql(database, "inbound_events_source_external_id_key")
    assert "CREATE UNIQUE INDEX" in unique_index.upper()
    assert "WHERE external_id IS NOT NULL" in unique_index
    assert "inbound_events_status_received_at_idx" in _index_names(database)


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

    assert _row_count(database) == 2


def test_database_constrains_status_attempts_and_blank_fields(
    database: Database, clock: FakeClock
) -> None:
    apply_migrations(database, clock=clock)

    with pytest.raises(sqlite3.IntegrityError, match="status_is_known"):
        _insert_raw(database, status="WEIRD", external_id="status-check")
    with pytest.raises(sqlite3.IntegrityError, match="attempts_not_negative"):
        _insert_raw(database, attempts=-1, external_id="attempts-check")
    with pytest.raises(sqlite3.IntegrityError, match="failure_carries_error"):
        _insert_raw(database, status="FAILED", external_id="failed-check")
    with pytest.raises(sqlite3.IntegrityError, match="source_not_blank"):
        _insert_raw(database, source="   ", external_id="source-check")


LEGACY_INSERT = """
INSERT INTO inbound_events (
    id, source, external_id, event_type, content,
    received_at, status, attempts, last_error, created_at, updated_at
) VALUES (
    :id, :source, :external_id, :event_type, :content,
    :received_at, :status, :attempts, :last_error, :created_at, :updated_at
)
"""


def test_upgrade_from_0001_preserves_existing_events(
    tmp_path: Path, clock: FakeClock
) -> None:
    """The 0002 rebuild must keep 0001 rows intact and only then add the new schema."""
    database = Database.at(tmp_path / "assistant.db")
    legacy_directory = tmp_path / "legacy"
    legacy_directory.mkdir()
    shipped_0001 = default_migrations_dir() / "0001_initial.sql"
    (legacy_directory / shipped_0001.name).write_text(
        shipped_0001.read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert [
        migration.version
        for migration in apply_migrations(database, clock=clock, directory=legacy_directory)
    ] == ["0001"]

    legacy_row = {
        "id": str(uuid4()),
        "source": "smail",
        "external_id": "uidvalidity1:uid99",
        "event_type": "mail.received",
        "content": "notice body",
        "received_at": NOW,
        "status": "FAILED",
        "attempts": 3,
        "last_error": "RuntimeError: boom",
        "created_at": NOW,
        "updated_at": NOW,
    }
    with database.connect() as connection:
        connection.execute(LEGACY_INSERT, legacy_row)

    applied = apply_migrations(database, clock=clock)

    assert [migration.version for migration in applied] == ["0002", "0003", "0004", "0005"]
    with database.connect() as connection:
        migrated = connection.execute(
            "SELECT * FROM inbound_events WHERE id = ?", (legacy_row["id"],)
        ).fetchone()
    assert migrated is not None
    for column in (
        "id",
        "source",
        "external_id",
        "event_type",
        "content",
        "received_at",
        "status",
        "attempts",
        "last_error",
        "created_at",
        "updated_at",
    ):
        assert migrated[column] == legacy_row[column], column
    for column in (
        "next_attempt_at",
        "claim_token",
        "claimed_by",
        "claimed_at",
        "lease_expires_at",
        "dead_lettered_at",
    ):
        assert migrated[column] is None, column

    # Indexes survive the rebuild: identity uniqueness and both claim-scan helpers.
    assert "inbound_events_status_deadlines_idx" in _index_names(database)
    unique_index = _index_sql(database, "inbound_events_source_external_id_key")
    assert "WHERE external_id IS NOT NULL" in unique_index
    with pytest.raises(sqlite3.IntegrityError):
        _insert_raw(database, external_id="uidvalidity1:uid99")

    # The status vocabulary grew to include DEAD_LETTERED, and nothing else.
    with database.connect() as connection:
        connection.execute(
            "UPDATE inbound_events SET status = 'DEAD_LETTERED', dead_lettered_at = ? WHERE id = ?",
            (NOW, legacy_row["id"]),
        )
    with (
        pytest.raises(sqlite3.IntegrityError, match="dead_letter_carries_timestamp"),
        database.connect() as connection,
    ):
        connection.execute(
            "UPDATE inbound_events SET status = 'DEAD_LETTERED', dead_lettered_at = NULL "
            "WHERE id = ?",
            (legacy_row["id"],),
        )
    with (
        pytest.raises(sqlite3.IntegrityError, match="status_is_known"),
        database.connect() as connection,
    ):
        connection.execute(
            "UPDATE inbound_events SET status = 'WEIRD' WHERE id = ?",
            (legacy_row["id"],),
        )

    # Re-running the migration set after the upgrade stays a no-op.
    assert apply_migrations(database, clock=clock) == ()
    assert applied_versions(database) == ("0001", "0002", "0003", "0004", "0005")


def test_upgraded_schema_rejects_a_half_written_lease(
    database: Database, clock: FakeClock
) -> None:
    apply_migrations(database, clock=clock)

    with (
        pytest.raises(sqlite3.IntegrityError, match="lease_is_complete"),
        database.connect() as connection,
    ):
        connection.execute(
            """
            INSERT INTO inbound_events (
                id, source, external_id, event_type, content, received_at, status,
                attempts, last_error, created_at, updated_at, claim_token
            ) VALUES (?, 'smail', 'lease-check', 'mail.received', NULL, ?, 'PROCESSING',
                      0, NULL, ?, ?, 'token-without-the-rest')
            """,
            (str(uuid4()), NOW, NOW, NOW),
        )


LEGACY_EVENT_INSERT = """
INSERT INTO inbound_events (
    id, source, external_id, event_type, content,
    received_at, status, attempts, last_error, created_at, updated_at
) VALUES (
    :id, :source, :external_id, :event_type, :content,
    :received_at, :status, :attempts, :last_error, :created_at, :updated_at
)
"""


def test_upgrade_from_v0_1_0_adds_the_storage_catalog(
    tmp_path: Path, clock: FakeClock
) -> None:
    """0003 adds the catalog tables without disturbing the event core."""
    database = Database.at(tmp_path / "assistant.db")
    legacy_directory = tmp_path / "legacy"
    legacy_directory.mkdir()
    shipped = default_migrations_dir()
    for name in ("0001_initial.sql", "0002_event_processing_leases.sql"):
        (legacy_directory / name).write_text(
            (shipped / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    assert [
        migration.version
        for migration in apply_migrations(database, clock=clock, directory=legacy_directory)
    ] == ["0001", "0002"]
    legacy_event = {
        "id": str(uuid4()),
        "source": "smail",
        "external_id": "uidvalidity1:uid7",
        "event_type": "mail.received",
        "content": "notice",
        "received_at": NOW,
        "status": "PROCESSING",
        "attempts": 1,
        "last_error": "RuntimeError: boom",
        "created_at": NOW,
        "updated_at": NOW,
    }
    with database.connect() as connection:
        connection.execute(LEGACY_EVENT_INSERT, legacy_event)

    applied = apply_migrations(database, clock=clock)

    assert [migration.version for migration in applied] == ["0003", "0004", "0005"]
    with database.connect() as connection:
        event_row = connection.execute(
            "SELECT * FROM inbound_events WHERE id = ?", (legacy_event["id"],)
        ).fetchone()
        tables = {
            str(row["name"])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        indexes = {
            str(row["name"])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
    assert event_row is not None
    for column, value in legacy_event.items():
        assert event_row[column] == value, column
    assert {"storage_roots", "catalog_entries"} <= tables
    assert "inbound_events_source_external_id_key" in indexes

    with database.connect() as connection:
        connection.execute(
            "INSERT INTO storage_roots (root_id, kind, label, last_known_path, "
            "first_seen_at, last_seen_at) VALUES ('archive-main', 'vault', 'Archive', "
            "'/mnt/e/archive', ?, ?)",
            (NOW, NOW),
        )
    with (
        pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"),
        database.connect() as connection,
    ):
        connection.execute(
            "INSERT INTO catalog_entries (id, root_id, relative_path, name, suffix, "
            "size_bytes, mtime_ns, media_type, presence, first_seen_at, last_seen_at, "
            "metadata_updated_at, last_seen_scan_id) VALUES "
            "(?, 'unknown-root', 'a.txt', 'a.txt', '.txt', 1, 1, NULL, 'present', ?, ?, ?, 's')",
            (str(uuid4()), NOW, NOW, NOW),
        )
    with (
        pytest.raises(sqlite3.IntegrityError, match="presence_is_known"),
        database.connect() as connection,
    ):
        connection.execute(
            "INSERT INTO catalog_entries (id, root_id, relative_path, name, suffix, "
            "size_bytes, mtime_ns, media_type, presence, first_seen_at, last_seen_at, "
            "metadata_updated_at, last_seen_scan_id) VALUES "
            "(?, 'archive-main', 'a.txt', 'a.txt', '.txt', 1, 1, NULL, 'offline', ?, ?, ?, 's')",
            (str(uuid4()), NOW, NOW, NOW),
        )
    with (
        pytest.raises(sqlite3.IntegrityError, match="size_not_negative"),
        database.connect() as connection,
    ):
        connection.execute(
            "INSERT INTO catalog_entries (id, root_id, relative_path, name, suffix, "
            "size_bytes, mtime_ns, media_type, presence, first_seen_at, last_seen_at, "
            "metadata_updated_at, last_seen_scan_id) VALUES "
            "(?, 'archive-main', 'a.txt', 'a.txt', '.txt', -1, 1, NULL, 'present', ?, ?, ?, 's')",
            (str(uuid4()), NOW, NOW, NOW),
        )

    with database.connect() as connection:
        connection.execute(
            "INSERT INTO catalog_entries (id, root_id, relative_path, name, suffix, "
            "size_bytes, mtime_ns, media_type, presence, first_seen_at, last_seen_at, "
            "metadata_updated_at, last_seen_scan_id) VALUES "
            "(?, 'archive-main', 'a.txt', 'a.txt', '.txt', 1, 1, NULL, 'present', ?, ?, ?, 's')",
            (str(uuid4()), NOW, NOW, NOW),
        )
    with (
        pytest.raises(sqlite3.IntegrityError, match="UNIQUE"),
        database.connect() as connection,
    ):
        connection.execute(
            "INSERT INTO catalog_entries (id, root_id, relative_path, name, suffix, "
            "size_bytes, mtime_ns, media_type, presence, first_seen_at, last_seen_at, "
            "metadata_updated_at, last_seen_scan_id) VALUES "
            "(?, 'archive-main', 'a.txt', 'a.txt', '.txt', 5, 5, NULL, 'present', ?, ?, ?, 's')",
            (str(uuid4()), NOW, NOW, NOW),
        )

    assert apply_migrations(database, clock=clock) == ()
    assert applied_versions(database) == ("0001", "0002", "0003", "0004", "0005")


def test_upgrade_from_v0_2_0_adds_the_commitment_core(
    tmp_path: Path, clock: FakeClock
) -> None:
    """0004 adds the commitment tables without disturbing events or the storage catalog."""
    database = Database.at(tmp_path / "assistant.db")
    legacy_directory = tmp_path / "legacy"
    legacy_directory.mkdir()
    shipped = default_migrations_dir()
    for name in (
        "0001_initial.sql",
        "0002_event_processing_leases.sql",
        "0003_storage_catalog.sql",
    ):
        (legacy_directory / name).write_text(
            (shipped / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    assert [
        migration.version
        for migration in apply_migrations(database, clock=clock, directory=legacy_directory)
    ] == ["0001", "0002", "0003"]
    event_row = {
        "id": str(uuid4()),
        "source": "smail",
        "external_id": "uidvalidity1:uid9",
        "event_type": "mail.received",
        "content": "notice",
        "received_at": NOW,
        "status": "RECEIVED",
        "attempts": 0,
        "last_error": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    with database.connect() as connection:
        connection.execute(LEGACY_EVENT_INSERT, event_row)
        connection.execute(
            "INSERT INTO storage_roots (root_id, kind, label, last_known_path, "
            "first_seen_at, last_seen_at) VALUES ('archive-main', 'vault', 'Archive', "
            "'/mnt/e/archive', ?, ?)",
            (NOW, NOW),
        )
        connection.execute(
            "INSERT INTO catalog_entries (id, root_id, relative_path, name, suffix, "
            "size_bytes, mtime_ns, media_type, presence, first_seen_at, last_seen_at, "
            "metadata_updated_at, last_seen_scan_id) VALUES "
            "(?, 'archive-main', 'notes/a.md', 'a.md', '.md', 10, 20, 'text/markdown', "
            "'present', ?, ?, ?, 'scan-1')",
            (str(uuid4()), NOW, NOW, NOW),
        )

    applied = apply_migrations(database, clock=clock)

    assert [migration.version for migration in applied] == ["0004", "0005"]
    with database.connect() as connection:
        stored_event = connection.execute(
            "SELECT * FROM inbound_events WHERE id = ?", (event_row["id"],)
        ).fetchone()
        stored_entry = connection.execute(
            "SELECT relative_path, size_bytes FROM catalog_entries"
        ).fetchone()
        tables = {
            str(row["name"])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert stored_event is not None
    for column, value in event_row.items():
        assert stored_event[column] == value, column
    assert stored_entry is not None
    assert (stored_entry["relative_path"], stored_entry["size_bytes"]) == ("notes/a.md", 10)
    assert {
        "tasks",
        "deadlines",
        "calendar_events",
        "plan_blocks",
        "work_sessions",
    } <= tables

    with (
        pytest.raises(sqlite3.IntegrityError, match="tasks_priority_is_known"),
        database.connect() as connection,
    ):
        connection.execute(
            "INSERT INTO tasks (id, title, status, priority, created_at, updated_at) "
            "VALUES (?, 'x', 'open', 'urgent', ?, ?)",
            (str(uuid4()), NOW, NOW),
        )
    with (
        pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"),
        database.connect() as connection,
    ):
        connection.execute(
            "INSERT INTO work_sessions (id, task_id, started_at, ended_at, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                str(uuid4()),
                str(uuid4()),
                NOW,
                "2026-09-20T10:00:00.000000+00:00",
                NOW,
            ),
        )

    assert apply_migrations(database, clock=clock) == ()
    assert applied_versions(database) == ("0001", "0002", "0003", "0004", "0005")


def test_upgrade_adds_planning_proposals_and_block_provenance(
    tmp_path: Path, clock: FakeClock
) -> None:
    """0005 rebuilds plan_blocks with provenance and adds the proposal tables."""
    database = Database.at(tmp_path / "assistant.db")
    legacy_directory = tmp_path / "legacy"
    legacy_directory.mkdir()
    shipped = default_migrations_dir()
    for name in (
        "0001_initial.sql",
        "0002_event_processing_leases.sql",
        "0003_storage_catalog.sql",
        "0004_commitment_core.sql",
    ):
        (legacy_directory / name).write_text(
            (shipped / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    apply_migrations(database, clock=clock, directory=legacy_directory)
    task_id = str(uuid4())
    block_id = str(uuid4())
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO tasks (id, title, status, priority, estimated_minutes, created_at, "
            "updated_at) VALUES (?, 'Write report', 'open', 'high', 300, ?, ?)",
            (task_id, NOW, NOW),
        )
        connection.execute(
            "INSERT INTO plan_blocks (id, task_id, starts_at, ends_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (block_id, task_id, NOW, "2026-09-20T11:00:00.000000+00:00", NOW, NOW),
        )

    applied = apply_migrations(database, clock=clock)

    assert [migration.version for migration in applied] == ["0005"]
    with database.connect() as connection:
        block = connection.execute(
            "SELECT origin, proposal_id, task_id FROM plan_blocks WHERE id = ?", (block_id,)
        ).fetchone()
        revision = connection.execute(
            "SELECT value FROM commitment_meta WHERE key = 'revision'"
        ).fetchone()
        tables = {
            str(row["name"])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert block is not None
    assert (block["origin"], block["proposal_id"], block["task_id"]) == ("manual", None, task_id)
    assert revision is not None and revision["value"] == "0"
    expected_tables = {
        "plan_proposals",
        "proposed_plan_blocks",
        "planning_issues",
        "commitment_meta",
    }
    assert expected_tables <= tables

    with (
        pytest.raises(sqlite3.IntegrityError, match="provenance_is_consistent"),
        database.connect() as connection,
    ):
        connection.execute(
            "INSERT INTO plan_blocks (id, task_id, starts_at, ends_at, created_at, updated_at, "
            "origin, proposal_id) VALUES (?, ?, ?, ?, ?, ?, 'planner', NULL)",
            (
                str(uuid4()),
                task_id,
                NOW,
                "2026-09-20T11:00:00.000000+00:00",
                NOW,
                NOW,
            ),
        )
    with (
        pytest.raises(sqlite3.IntegrityError, match="origin_is_known"),
        database.connect() as connection,
    ):
        connection.execute(
            "INSERT INTO plan_blocks (id, task_id, starts_at, ends_at, created_at, updated_at, "
            "origin, proposal_id) VALUES (?, ?, ?, ?, ?, ?, 'robot', NULL)",
            (
                str(uuid4()),
                task_id,
                NOW,
                "2026-09-20T11:00:00.000000+00:00",
                NOW,
                NOW,
            ),
        )

    assert apply_migrations(database, clock=clock) == ()
    assert applied_versions(database) == ("0001", "0002", "0003", "0004", "0005")
