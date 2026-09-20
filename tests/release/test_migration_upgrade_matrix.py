"""Every historical schema this project shipped upgrades to today's (ADR-0032 §15/§19/§20/§21).

The project has shipped fifteen forward migrations. A v1 user may restore a runtime from any of
those eras, or open a laptop that has not run the daemon since v0.2 — so "upgrade works" has to mean
every prefix, not the one path the developer happened to try.

Three things are checked here:

* **every prefix upgrades.** A database created with migrations `0001…N` applies the rest and ends
  up with today's schema, integrity and foreign keys intact;
* **representative eras keep their data.** Six prefixes (`0001`, `0003`, `0006`, `0009`, `0012`,
  `0015`) are seeded with rows of their era — an event, a storage root, a commitment with a
  reminder, mail and a draft, cases and actions, facts and observations — and those rows are still
  there, unchanged, after the upgrade;
* **a newer database fails closed.** A `schema_migrations` row this build does not ship (a future
  migration, or a rewritten name for a version that does exist) is a refusal, not something to
  ignore.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from assistant.store.db import Database
from assistant.store.errors import DatabaseMigrationIncompatible
from assistant.store.migrations import (
    applied_versions,
    apply_migrations,
    require_compatible_history,
)
from tests.support.fakes import FakeClock
from tests.support.upgrades import (
    ALL_VERSIONS,
    REPRESENTATIVE_PREFIXES,
    STAMP,
    count_row,
    database_at_prefix,
    insert,
    seed_era,
)

NOW = datetime(2026, 9, 26, 9, 0, tzinfo=UTC)
EVERY_PREFIX = tuple(ALL_VERSIONS)
REPRESENTATIVE = REPRESENTATIVE_PREFIXES


@pytest.mark.parametrize("prefix", EVERY_PREFIX)
def test_every_migration_prefix_upgrades_to_the_current_schema(
    tmp_path: Path, prefix: str
) -> None:
    """No intermediate historical state is a dead end: every prefix reaches today's schema."""
    clock = FakeClock(start=NOW)
    database = database_at_prefix(tmp_path, prefix, clock)

    applied = apply_migrations(database, clock=clock)

    assert tuple(item.version for item in applied) == tuple(
        version for version in ALL_VERSIONS if version > prefix
    )
    assert applied_versions(database) == ALL_VERSIONS
    with database.connect() as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("prefix", REPRESENTATIVE)
def test_a_representative_old_runtime_keeps_its_data_through_the_upgrade(
    tmp_path: Path, prefix: str
) -> None:
    """Rows written by an older build are still there, and still readable, after the upgrade."""
    clock = FakeClock(start=NOW)
    database = database_at_prefix(tmp_path, prefix, clock)
    with database.connect() as connection:
        seeded = seed_era(connection, prefix)

    apply_migrations(database, clock=clock)
    require_compatible_history(database)  # the upgraded database is one this build understands

    with database.connect() as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        for table, identifier in seeded.items():
            assert count_row(connection, table, identifier) == 1, table
        # The schema is not merely openable: a table introduced after this era accepts a new row.
        insert(
            connection,
            "inbound_events",
            id=f"event-after-{prefix}",
            source="manual:manual",
            external_id=None,
            event_type="manual.input.received",
            content="{}",
            received_at=STAMP,
            status="RECEIVED",
            attempts=0,
            last_error=None,
            created_at=STAMP,
            updated_at=STAMP,
            next_attempt_at=None,
            claim_token=None,
            claimed_by=None,
            claimed_at=None,
            lease_expires_at=None,
        )
        assert count_row(connection, "inbound_events", f"event-after-{prefix}") == 1


def test_a_database_from_a_newer_binary_fails_closed(tmp_path: Path) -> None:
    """§19: an applied migration this build does not ship is a refusal, never ignored."""
    clock = FakeClock(start=NOW)
    database = database_at_prefix(tmp_path, "0015", clock)
    with database.connect() as connection:
        insert(
            connection,
            "schema_migrations",
            version="9999",
            name="9999_future_feature.sql",
            applied_at=STAMP,
        )

    with pytest.raises(DatabaseMigrationIncompatible) as failure:
        require_compatible_history(database)

    assert "9999_future_feature.sql" not in str(failure.value)  # names, not history, are reported
    assert "9999" in str(failure.value)
    assert "not supported" in str(failure.value)


def test_a_rewritten_historical_migration_is_refused(tmp_path: Path) -> None:
    """§14: the migrations are immutable, so a known version with a different name is a refusal."""
    clock = FakeClock(start=NOW)
    database = database_at_prefix(tmp_path, "0015", clock)
    with database.connect() as connection:
        connection.execute(
            "UPDATE schema_migrations SET name = ? WHERE version = ?",
            ("0015_something_else.sql", "0015"),
        )

    with pytest.raises(DatabaseMigrationIncompatible):
        require_compatible_history(database)


def test_an_unmigrated_database_has_no_history_to_disagree_with(tmp_path: Path) -> None:
    """A fresh file (no `schema_migrations` table) is not an incompatible database."""
    database = Database.at(tmp_path / "fresh" / "assistant.db")

    require_compatible_history(database)

    with database.connect() as connection:
        created = connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'schema_migrations'"
        ).fetchall()
    # The check read the file; it did not migrate it, and it left no bookkeeping table behind.
    assert created == []
