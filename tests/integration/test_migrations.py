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
        "0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009",
        "0010", "0011", "0012", "0013", "0014", "0015", "0016",
        "0017", "0018", "0019", "0020", "0021", "0022", "0023",
    ]
    assert [migration.name for migration in applied] == [
        "0001_initial.sql",
        "0002_event_processing_leases.sql",
        "0003_storage_catalog.sql",
        "0004_commitment_core.sql",
        "0005_planning_proposals.sql",
        "0006_scheduler_notifications.sql",
        "0007_inbound_mail.sql",
        "0008_mail_intelligence.sql",
        "0009_mail_reply_drafts.sql",
        "0010_case_action_approval.sql",
        "0011_approved_mail_send.sql",
        "0012_mobile_web.sql",
        "0013_learning_facts.sql",
        "0014_playbooks.sql",
        "0015_inbound_observations.sql",
        "0016_conversations.sql",
        "0017_conversation_external_reviews.sql",
        "0018_recurring_calendar_rules.sql",
        "0019_contacts_and_outbound_mail.sql",
        "0020_conversation_requests.sql",
        "0021_attention_items.sql",
        "0022_planning_preferences.sql",
        "0023_conversation_review_expansion.sql",
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
    assert applied_versions(database) == (
        "0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009", "0010",
        "0011", "0012", "0013", "0014", "0015", "0016", "0017",
        "0018", "0019", "0020", "0021", "0022", "0023",
    )


def test_upgrade_adds_the_observation_tables(tmp_path: Path, clock: FakeClock) -> None:
    """0015 adds watcher state, observations, manual input and analyses, preserving old rows."""
    database = Database.at(tmp_path / "assistant.db")
    legacy_directory = tmp_path / "legacy"
    legacy_directory.mkdir()
    shipped = default_migrations_dir()
    for name in (
        "0001_initial.sql",
        "0002_event_processing_leases.sql",
        "0003_storage_catalog.sql",
        "0004_commitment_core.sql",
        "0005_planning_proposals.sql",
        "0006_scheduler_notifications.sql",
        "0007_inbound_mail.sql",
        "0008_mail_intelligence.sql",
        "0009_mail_reply_drafts.sql",
        "0010_case_action_approval.sql",
        "0011_approved_mail_send.sql",
        "0012_mobile_web.sql",
        "0013_learning_facts.sql",
        "0014_playbooks.sql",
    ):
        (legacy_directory / name).write_text(
            (shipped / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    apply_migrations(database, clock=clock, directory=legacy_directory)

    event_id = str(uuid4())
    with database.connect() as connection:
        connection.execute(RAW_INSERT, _raw_event(event_id))
        connection.execute(
            "INSERT INTO mail_messages (id, account_id, message_id_header, references_json, "
            "to_addresses_json, cc_addresses_json, reply_to_addresses_json, body_status, "
            "content_fingerprint, size_bytes, parse_warnings, first_seen_at, last_seen_at) "
            "VALUES (?, 'smail', '<a@example.edu>', '[]', '[]', '[]', '[]', 'available', ?, "
            "10, 0, ?, ?)",
            (str(uuid4()), "a" * 64, NOW, NOW),
        )

    applied = apply_migrations(database, clock=clock)

    assert [migration.version for migration in applied] == [
        "0015",
        "0016",
        "0017",
        "0018",
        "0019",
        "0020", "0021", "0022", "0023",
    ]
    assert {
        "web_watch_state",
        "web_observations",
        "web_observation_event_links",
        "manual_inputs",
        "manual_input_event_links",
        "observation_analyses",
    } <= _table_names(database)
    with database.connect() as connection:
        events = connection.execute("SELECT count(*) AS total FROM inbound_events").fetchone()
        messages = connection.execute("SELECT count(*) AS total FROM mail_messages").fetchone()
    assert events["total"] == 1 and messages["total"] == 1

    _assert_observation_constraints(database, event_id=event_id)

    assert apply_migrations(database, clock=clock) == ()
    assert applied_versions(database) == (
        "0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009", "0010",
        "0011", "0012", "0013", "0014", "0015", "0016", "0017",
        "0018", "0019", "0020", "0021", "0022", "0023",
    )


def _raw_event(event_id: str) -> dict[str, object]:
    values: dict[str, object] = {
        "id": event_id,
        "source": "cli",
        "external_id": None,
        "event_type": "local.note",
        "content": None,
        "received_at": NOW,
        "status": "RECEIVED",
        "attempts": 0,
        "last_error": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return values


def _assert_observation_constraints(database: Database, *, event_id: str) -> None:
    """§48: the schema refuses a bad hash, a bad status, a bad counter and a dangling reference."""
    observation_id = str(uuid4())
    insert_observation = (
        "INSERT INTO web_observations (id, target_id, url, content_sha256, storage_key, "
        "previous_observation_id, is_baseline, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    )
    with database.connect() as connection:
        connection.execute(
            insert_observation,
            (
                observation_id,
                "course-notices",
                "https://example.edu/notices",
                "a" * 64,
                "web/snapshots/aa/aaa.txt",
                None,
                1,
                NOW,
            ),
        )
        # A baseline cannot claim to replace something; a change must name what it replaced.
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                insert_observation,
                (
                    str(uuid4()),
                    "course-notices",
                    "https://example.edu/notices",
                    "b" * 64,
                    "web/snapshots/bb/bbb.txt",
                    observation_id,
                    1,
                    NOW,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                insert_observation,
                (
                    str(uuid4()),
                    "course-notices",
                    "https://example.edu/notices",
                    "b" * 64,
                    "web/snapshots/bb/bbb.txt",
                    None,
                    0,
                    NOW,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                insert_observation,
                (
                    str(uuid4()),
                    "course-notices",
                    "https://example.edu/notices",
                    "short",
                    "web/snapshots/bb/bbb.txt",
                    None,
                    1,
                    NOW,
                ),
            )
        # The state points at real observations and refuses a negative counter.
        connection.execute(
            "INSERT INTO web_watch_state (target_id, url, content_sha256, "
            "latest_observation_id, etag, last_modified, checks_since_full, last_checked_at, "
            "last_changed_at, updated_at) VALUES (?, ?, ?, ?, NULL, NULL, 0, ?, NULL, ?)",
            (
                "course-notices",
                "https://example.edu/notices",
                "a" * 64,
                observation_id,
                NOW,
                NOW,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                "UPDATE web_watch_state SET checks_since_full = -1 WHERE target_id = ?",
                ("course-notices",),
            )
        # The bridge rows point at real events and real sources.
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                "INSERT INTO web_observation_event_links "
                "(observation_id, inbound_event_id, linked_at) VALUES (?, ?, ?)",
                (observation_id, str(uuid4()), NOW),
            )
        connection.execute(
            "INSERT INTO web_observation_event_links "
            "(observation_id, inbound_event_id, linked_at) VALUES (?, ?, ?)",
            (observation_id, event_id, NOW),
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                "INSERT INTO web_observation_event_links "
                "(observation_id, inbound_event_id, linked_at) VALUES (?, ?, ?)",
                (observation_id, event_id, NOW),
            )
        # Manual input is a closed source vocabulary with bounded text.
        connection.execute(
            "INSERT INTO manual_inputs (id, source, text, content_sha256, created_at) "
            "VALUES (?, 'qq-forward', 'a note', ?, ?)",
            (str(uuid4()), "c" * 64, NOW),
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                "INSERT INTO manual_inputs (id, source, text, content_sha256, created_at) "
                "VALUES (?, 'shell', 'a note', ?, ?)",
                (str(uuid4()), "c" * 64, NOW),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                "INSERT INTO manual_inputs (id, source, text, content_sha256, created_at) "
                "VALUES (?, 'manual', '   ', ?, ?)",
                (str(uuid4()), "c" * 64, NOW),
            )
        # One analysis per event, with a closed category vocabulary and a bounded summary.
        connection.execute(
            "INSERT INTO observation_analyses (id, inbound_event_id, source_kind, "
            "analyzer_version, input_fingerprint, category, summary, action_candidates_json, "
            "created_at, updated_at) VALUES (?, ?, 'manual.input.received', 1, ?, 'ignore', "
            "'nothing to do', '[]', ?, ?)",
            (str(uuid4()), event_id, "d" * 64, NOW, NOW),
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                "INSERT INTO observation_analyses (id, inbound_event_id, source_kind, "
                "analyzer_version, input_fingerprint, category, summary, "
                "action_candidates_json, created_at, updated_at) VALUES (?, ?, "
                "'manual.input.received', 1, ?, 'ignore', 'again', '[]', ?, ?)",
                (str(uuid4()), event_id, "e" * 64, NOW, NOW),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                "INSERT INTO observation_analyses (id, inbound_event_id, source_kind, "
                "analyzer_version, input_fingerprint, category, summary, "
                "action_candidates_json, created_at, updated_at) VALUES (?, ?, "
                "'manual.input.received', 1, ?, 'urgent', 'x', '[]', ?, ?)",
                (str(uuid4()), str(uuid4()), "f" * 64, NOW, NOW),
            )


def test_upgrade_adds_the_playbook_tables(tmp_path: Path, clock: FakeClock) -> None:
    """0014 adds candidates, replay tests and playbooks without touching an existing row."""
    database = Database.at(tmp_path / "assistant.db")
    legacy_directory = tmp_path / "legacy"
    legacy_directory.mkdir()
    shipped = default_migrations_dir()
    for name in (
        "0001_initial.sql",
        "0002_event_processing_leases.sql",
        "0003_storage_catalog.sql",
        "0004_commitment_core.sql",
        "0005_planning_proposals.sql",
        "0006_scheduler_notifications.sql",
        "0007_inbound_mail.sql",
        "0008_mail_intelligence.sql",
        "0009_mail_reply_drafts.sql",
        "0010_case_action_approval.sql",
        "0011_approved_mail_send.sql",
        "0012_mobile_web.sql",
        "0013_learning_facts.sql",
    ):
        (legacy_directory / name).write_text(
            (shipped / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    apply_migrations(database, clock=clock, directory=legacy_directory)

    case_id = str(uuid4())
    action_id = str(uuid4())
    approval_id = str(uuid4())
    run_id = str(uuid4())
    correction_id = str(uuid4())
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO cases (id, title, status, created_at, updated_at, completed_at, "
            "cancelled_at) VALUES (?, 'Send the certificate', 'open', ?, ?, NULL, NULL)",
            (case_id, NOW, NOW),
        )
        connection.execute(
            "INSERT INTO action_requests (id, case_id, action_type, payload_json, fingerprint, "
            "status, created_at, executed_at, cancelled_at) VALUES (?, ?, "
            "'ehall.submit-certificate', '{\"a\":1}', ?, 'executed', ?, ?, NULL)",
            (action_id, case_id, "c" * 64, NOW, NOW),
        )
        connection.execute(
            "INSERT INTO approvals (id, action_id, action_fingerprint, approved_at, expires_at, "
            "consumed_at, superseded_at) VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (
                approval_id,
                action_id,
                "c" * 64,
                NOW,
                "2026-09-19T12:10:00.000000+00:00",
                NOW,
            ),
        )
        connection.execute(
            "INSERT INTO execution_runs (id, action_id, approval_id, status, started_at, "
            "finished_at, error_summary) VALUES (?, ?, ?, 'succeeded', ?, ?, NULL)",
            (run_id, action_id, approval_id, NOW, NOW),
        )
        connection.execute(
            "INSERT INTO corrections (id, text, created_at) VALUES (?, 'my office is 302', ?)",
            (correction_id, NOW),
        )

    applied = apply_migrations(database, clock=clock)

    assert [migration.version for migration in applied] == [
        "0014", "0015", "0016", "0017", "0018", "0019", "0020", "0021", "0022", "0023",
    ]
    assert {
        "playbook_candidates",
        "playbook_replay_tests",
        "playbooks",
    } <= _table_names(database)
    with database.connect() as connection:
        actions = connection.execute("SELECT count(*) AS total FROM action_requests").fetchone()
        runs = connection.execute("SELECT count(*) AS total FROM execution_runs").fetchone()
        corrections = connection.execute("SELECT count(*) AS total FROM corrections").fetchone()
    assert actions["total"] == 1 and runs["total"] == 1 and corrections["total"] == 1

    _assert_playbook_constraints(
        database, action_id=action_id, run_id=run_id, unrelated_action_id=str(uuid4())
    )

    assert apply_migrations(database, clock=clock) == ()
    assert applied_versions(database) == (
        "0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009", "0010",
        "0011", "0012", "0013", "0014", "0015", "0016", "0017",
        "0018", "0019", "0020", "0021", "0022", "0023",
    )


def _assert_playbook_constraints(
    database: Database, *, action_id: str, run_id: str, unrelated_action_id: str
) -> None:
    """§48: the schema refuses a duplicated source, a bad status and a dangling reference."""
    candidate_id = str(uuid4())
    spare_candidate_id = str(uuid4())
    test_id = str(uuid4())
    insert_candidate = (
        "INSERT INTO playbook_candidates (id, name, note, source_action_id, "
        "source_execution_run_id, source_action_type, source_action_fingerprint, status, "
        "created_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    insert_test = (
        "INSERT INTO playbook_replay_tests (id, candidate_id, action_type, contract_version, "
        "input_fingerprint, status, issue_codes_json, tested_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    )
    insert_playbook = (
        "INSERT INTO playbooks (id, candidate_id, name, note, action_type, source_action_id, "
        "source_execution_run_id, source_action_fingerprint, replay_contract_version, "
        "promoted_from_test_id, status, created_at, retired_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    with database.connect() as connection:
        # A second successful source, so the "dangling promotion test" case has its own candidate.
        spare_action_id, spare_run_id = str(uuid4()), str(uuid4())
        spare_approval_id = str(uuid4())
        connection.execute(
            "INSERT INTO action_requests (id, case_id, action_type, payload_json, fingerprint, "
            "status, created_at, executed_at, cancelled_at) SELECT ?, case_id, action_type, "
            "payload_json, ?, 'executed', created_at, ?, NULL FROM action_requests WHERE id = ?",
            (spare_action_id, "e" * 64, NOW, action_id),
        )
        connection.execute(
            "INSERT INTO approvals (id, action_id, action_fingerprint, approved_at, expires_at, "
            "consumed_at, superseded_at) VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (
                spare_approval_id,
                spare_action_id,
                "e" * 64,
                NOW,
                "2026-09-19T12:10:00.000000+00:00",
                NOW,
            ),
        )
        connection.execute(
            "INSERT INTO execution_runs (id, action_id, approval_id, status, started_at, "
            "finished_at, error_summary) VALUES (?, ?, ?, 'succeeded', ?, ?, NULL)",
            (spare_run_id, spare_action_id, spare_approval_id, NOW, NOW),
        )
        connection.execute(
            insert_candidate,
            (
                spare_candidate_id,
                "Spare",
                "reviewed",
                spare_action_id,
                spare_run_id,
                "mail.send",
                "e" * 64,
                "pending",
                NOW,
                None,
            ),
        )
        # Provenance is a real foreign key: an action that does not exist cannot be reviewed.
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                insert_candidate,
                (
                    str(uuid4()),
                    "Ghost",
                    "reviewed",
                    unrelated_action_id,
                    run_id,
                    "mail.send",
                    "a" * 64,
                    "pending",
                    NOW,
                    None,
                ),
            )
        connection.execute(
            insert_candidate,
            (
                candidate_id,
                "Approved certificate workflow",
                "Reviewed the successful run.",
                action_id,
                run_id,
                "ehall.submit-certificate",
                "c" * 64,
                "pending",
                NOW,
                None,
            ),
        )
        # One successful action can seed one candidate only.
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                insert_candidate,
                (
                    str(uuid4()),
                    "Second try",
                    "reviewed",
                    action_id,
                    run_id,
                    "ehall.submit-certificate",
                    "c" * 64,
                    "pending",
                    NOW,
                    None,
                ),
            )
        # The status vocabulary, the blank-name rule and the pending/resolved agreement hold.
        for overrides in (
            {"status": "maybe", "resolved": None},
            {"status": "pending", "resolved": NOW},
            {"status": "promoted", "resolved": None},
        ):
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                connection.execute(
                    insert_candidate,
                    (
                        str(uuid4()),
                        "x",
                        "y",
                        action_id,
                        run_id,
                        "mail.send",
                        "a" * 64,
                        overrides["status"],
                        NOW,
                        overrides["resolved"],
                    ),
                )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                insert_candidate,
                (
                    str(uuid4()),
                    "   ",
                    "y",
                    action_id,
                    run_id,
                    "mail.send",
                    "a" * 64,
                    "pending",
                    NOW,
                    None,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                insert_candidate,
                (
                    str(uuid4()),
                    "x",
                    "y",
                    action_id,
                    run_id,
                    "mail.send",
                    "short",
                    "pending",
                    NOW,
                    None,
                ),
            )
        # A passing test carries no issue codes; a failing one carries at least one.
        connection.execute(
            insert_test,
            (
                test_id,
                candidate_id,
                "ehall.submit-certificate",
                1,
                "d" * 64,
                "passed",
                "[]",
                NOW,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                insert_test,
                (
                    str(uuid4()),
                    candidate_id,
                    "ehall.submit-certificate",
                    1,
                    "d" * 64,
                    "passed",
                    '["payload-invalid"]',
                    NOW,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                insert_test,
                (
                    str(uuid4()),
                    candidate_id,
                    "ehall.submit-certificate",
                    1,
                    "d" * 64,
                    "failed",
                    "[]",
                    NOW,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                insert_test,
                (
                    str(uuid4()),
                    candidate_id,
                    "ehall.submit-certificate",
                    0,
                    "d" * 64,
                    "passed",
                    "[]",
                    NOW,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                insert_test,
                (
                    str(uuid4()),
                    str(uuid4()),
                    "ehall.submit-certificate",
                    1,
                    "d" * 64,
                    "passed",
                    "[]",
                    NOW,
                ),
            )
        # A playbook points at the candidate, the qualifying test and the original source.
        playbook_id = str(uuid4())
        connection.execute(
            insert_playbook,
            (
                playbook_id,
                candidate_id,
                "Approved certificate workflow",
                "Reviewed and tested.",
                "ehall.submit-certificate",
                action_id,
                run_id,
                "c" * 64,
                1,
                test_id,
                "active",
                NOW,
                None,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                insert_playbook,
                (
                    str(uuid4()),
                    candidate_id,
                    "Again",
                    "Reviewed and tested.",
                    "ehall.submit-certificate",
                    action_id,
                    run_id,
                    "c" * 64,
                    1,
                    test_id,
                    "active",
                    NOW,
                    None,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                insert_playbook,
                (
                    str(uuid4()),
                    spare_candidate_id,
                    "Orphan",
                    "Reviewed and tested.",
                    "ehall.submit-certificate",
                    action_id,
                    run_id,
                    "e" * 64,
                    1,
                    str(uuid4()),
                    "active",
                    NOW,
                    None,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                insert_playbook,
                (
                    str(uuid4()),
                    candidate_id,
                    "Half retired",
                    "Reviewed and tested.",
                    "ehall.submit-certificate",
                    action_id,
                    run_id,
                    "c" * 64,
                    1,
                    test_id,
                    "retired",
                    NOW,
                    None,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                insert_playbook,
                (
                    str(uuid4()),
                    candidate_id,
                    "Confused",
                    "Reviewed and tested.",
                    "ehall.submit-certificate",
                    action_id,
                    run_id,
                    "c" * 64,
                    1,
                    test_id,
                    "active",
                    NOW,
                    NOW,
                ),
            )


def test_upgrade_adds_the_learning_tables(tmp_path: Path, clock: FakeClock) -> None:
    """0013 adds corrections, candidates and confirmed facts without touching an existing row."""
    database = Database.at(tmp_path / "assistant.db")
    legacy_directory = tmp_path / "legacy"
    legacy_directory.mkdir()
    shipped = default_migrations_dir()
    for name in (
        "0001_initial.sql",
        "0002_event_processing_leases.sql",
        "0003_storage_catalog.sql",
        "0004_commitment_core.sql",
        "0005_planning_proposals.sql",
        "0006_scheduler_notifications.sql",
        "0007_inbound_mail.sql",
        "0008_mail_intelligence.sql",
        "0009_mail_reply_drafts.sql",
        "0010_case_action_approval.sql",
        "0011_approved_mail_send.sql",
        "0012_mobile_web.sql",
    ):
        (legacy_directory / name).write_text(
            (shipped / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    apply_migrations(database, clock=clock, directory=legacy_directory)

    message_id = str(uuid4())
    draft_id = str(uuid4())
    session_id = str(uuid4())
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO mail_messages (id, account_id, message_id_header, references_json, "
            "to_addresses_json, cc_addresses_json, reply_to_addresses_json, body_status, "
            "content_fingerprint, size_bytes, parse_warnings, first_seen_at, last_seen_at) "
            "VALUES (?, 'smail', '<a@example.edu>', '[]', '[]', '[]', '[]', 'available', ?, "
            "10, 0, ?, ?)",
            (message_id, "a" * 64, NOW, NOW),
        )
        connection.execute(
            "INSERT INTO mail_drafts (id, account_id, reply_to_message_id, to_addresses_json, "
            "subject, body_text, needs_user_input_json, origin, version, "
            "generation_input_fingerprint, prompt_version, created_at, updated_at) "
            "VALUES (?, 'smail', ?, '[\"ada@example.edu\"]', 'Re: x', 'body', '[]', "
            "'model_generated', 1, ?, 1, ?, ?)",
            (draft_id, message_id, "b" * 64, NOW, NOW),
        )
        connection.execute(
            "INSERT INTO mobile_sessions (id, session_hash, csrf_hash, created_at, expires_at, "
            "last_seen_at, revoked_at) VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (
                session_id,
                "f" * 64,
                "0" * 64,
                NOW,
                "2026-10-19T12:00:00.000000+00:00",
                NOW,
            ),
        )

    applied = apply_migrations(database, clock=clock)

    assert [migration.version for migration in applied] == [
        "0013", "0014", "0015", "0016", "0017", "0018", "0019", "0020", "0021", "0022", "0023",
    ]
    assert {"corrections", "fact_candidates", "confirmed_facts"} <= _table_names(database)
    assert "confirmed_facts_current_idx" in _index_names(database)
    with database.connect() as connection:
        drafts = connection.execute("SELECT count(*) AS total FROM mail_drafts").fetchone()
        sessions = connection.execute(
            "SELECT count(*) AS total FROM mobile_sessions"
        ).fetchone()
    assert drafts["total"] == 1 and sessions["total"] == 1  # older data survived

    _assert_learning_constraints(database)

    assert apply_migrations(database, clock=clock) == ()
    assert applied_versions(database) == (
        "0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009", "0010",
        "0011", "0012", "0013", "0014", "0015", "0016", "0017",
        "0018", "0019", "0020", "0021", "0022", "0023",
    )


def _assert_learning_constraints(database: Database) -> None:
    """§32: the schema itself refuses a bad key, a bad status and an incoherent resolution."""
    correction_id = str(uuid4())
    candidate_id = str(uuid4())
    fact_id = str(uuid4())
    insert_correction = (
        "INSERT INTO corrections (id, text, created_at) VALUES (?, ?, ?)"
    )
    insert_candidate = (
        "INSERT INTO fact_candidates (id, fact_key, value, correction_id, status, "
        "proposed_valid_until, created_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    )
    insert_fact = (
        "INSERT INTO confirmed_facts (id, candidate_id, fact_key, value, valid_from, "
        "valid_until, created_at, superseded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    )
    later = "2026-09-20T13:00:00.000000+00:00"
    with database.connect() as connection:
        connection.execute(insert_correction, (correction_id, "my office is 302", NOW))
        # A blank correction is refused before anything can depend on it.
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(insert_correction, (str(uuid4()), "   ", NOW))
        # Provenance is a real foreign key, not a convention.
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                insert_candidate,
                (str(uuid4()), "profile.office", "Room 302", str(uuid4()), "pending",
                 None, NOW, None),
            )
        # A key that is not a lowercase namespaced identifier never reaches the table.
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                insert_candidate,
                (str(uuid4()), "Profile.Password", "x", correction_id, "pending",
                 None, NOW, None),
            )
        # The status vocabulary is closed.
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                insert_candidate,
                (str(uuid4()), "profile.office", "Room 302", correction_id, "maybe",
                 None, NOW, None),
            )
        # A resolved candidate has a timestamp; a pending one does not.
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                insert_candidate,
                (candidate_id, "profile.office", "Room 302", correction_id, "pending",
                 None, NOW, later),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                insert_candidate,
                (str(uuid4()), "profile.office", "Room 302", correction_id, "confirmed",
                 None, NOW, None),
            )
        # The proposed window has to follow creation.
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                insert_candidate,
                (str(uuid4()), "profile.office", "Room 320", correction_id, "pending",
                 "2026-09-19T11:00:00.000000+00:00", NOW, None),
            )
        connection.execute(
            insert_candidate,
            (candidate_id, "profile.office", "Room 302", correction_id, "confirmed", None,
             NOW, later),
        )
        # A confirmed fact points at a real candidate, and only once.
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                insert_fact,
                (str(uuid4()), str(uuid4()), "profile.office", "Room 302", NOW, None, NOW,
                 None),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                insert_fact,
                (fact_id, candidate_id, "profile.office", "Room 302", later,
                 "2026-09-20T12:30:00.000000+00:00", later, None),
            )
        connection.execute(
            insert_fact, (fact_id, candidate_id, "profile.office", "Room 302", NOW, None, NOW, None)
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                insert_fact,
                (str(uuid4()), candidate_id, "profile.office", "Room 302", NOW, None, NOW,
                 later),
            )


def test_upgrade_adds_the_mobile_tables(tmp_path: Path, clock: FakeClock) -> None:
    """0012 adds pairing and session storage without touching one existing row.

    Nothing in the mobile tables is a credential: both are 64-character SHA-256 columns with a
    `CHECK`, so the schema itself refuses to hold a usable secret even if application code tried.
    """
    database = Database.at(tmp_path / "assistant.db")
    legacy_directory = tmp_path / "legacy"
    legacy_directory.mkdir()
    shipped = default_migrations_dir()
    for name in (
        "0001_initial.sql",
        "0002_event_processing_leases.sql",
        "0003_storage_catalog.sql",
        "0004_commitment_core.sql",
        "0005_planning_proposals.sql",
        "0006_scheduler_notifications.sql",
        "0007_inbound_mail.sql",
        "0008_mail_intelligence.sql",
        "0009_mail_reply_drafts.sql",
        "0010_case_action_approval.sql",
        "0011_approved_mail_send.sql",
    ):
        (legacy_directory / name).write_text(
            (shipped / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    apply_migrations(database, clock=clock, directory=legacy_directory)

    message_id = str(uuid4())
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO mail_messages (id, account_id, message_id_header, references_json, "
            "to_addresses_json, cc_addresses_json, reply_to_addresses_json, body_status, "
            "content_fingerprint, size_bytes, parse_warnings, first_seen_at, last_seen_at) "
            "VALUES (?, 'smail', '<a@example.edu>', '[]', '[]', '[]', '[]', 'available', ?, "
            "10, 0, ?, ?)",
            (message_id, "a" * 64, NOW, NOW),
        )
        connection.execute(
            "INSERT INTO mail_drafts (id, account_id, reply_to_message_id, to_addresses_json, "
            "subject, body_text, needs_user_input_json, origin, version, "
            "generation_input_fingerprint, prompt_version, created_at, updated_at) "
            "VALUES (?, 'smail', ?, '[\"ada@example.edu\"]', 'Re: x', 'body', '[]', "
            "'model_generated', 1, ?, 1, ?, ?)",
            (str(uuid4()), message_id, "b" * 64, NOW, NOW),
        )

    applied = apply_migrations(database, clock=clock)

    assert [migration.version for migration in applied] == [
        "0012", "0013", "0014", "0015", "0016", "0017", "0018", "0019", "0020", "0021",
        "0022", "0023",
    ]
    assert {"mobile_pairing_tokens", "mobile_sessions"} <= _table_names(database)
    assert {
        "mobile_pairing_tokens_hash_idx",
        "mobile_sessions_hash_idx",
        "mobile_sessions_expiry_idx",
    } <= _index_names(database)
    with database.connect() as connection:
        drafts = connection.execute("SELECT count(*) AS total FROM mail_drafts").fetchone()
        messages = connection.execute("SELECT count(*) AS total FROM mail_messages").fetchone()
    assert drafts["total"] == 1 and messages["total"] == 1  # older data survived

    _assert_mobile_constraints(database, clock=clock)

    assert apply_migrations(database, clock=clock) == ()
    assert applied_versions(database) == (
        "0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009",
        "0010", "0011", "0012", "0013", "0014", "0015", "0016",
        "0017", "0018", "0019", "0020", "0021", "0022", "0023",
    )


def _assert_mobile_constraints(database: Database, *, clock: FakeClock) -> None:
    """Hashes are unique, one-time state is monotone, and a short value cannot be stored."""
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO mobile_pairing_tokens (id, token_hash, created_at, expires_at, "
            "consumed_at) VALUES (?, ?, ?, ?, NULL)",
            (str(uuid4()), "d" * 64, NOW, "2026-09-19T12:10:00.000000+00:00"),
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                "INSERT INTO mobile_pairing_tokens (id, token_hash, created_at, expires_at, "
                "consumed_at) VALUES (?, ?, ?, ?, NULL)",
                (str(uuid4()), "d" * 64, NOW, "2026-09-19T12:10:00.000000+00:00"),
            )
        # The `CHECK` is what makes the plaintext impossible, not the application's manners.
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                "INSERT INTO mobile_pairing_tokens (id, token_hash, created_at, expires_at, "
                "consumed_at) VALUES (?, ?, ?, ?, NULL)",
                (str(uuid4()), "SHORT", NOW, "2026-09-19T12:10:00.000000+00:00"),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                "INSERT INTO mobile_pairing_tokens (id, token_hash, created_at, expires_at, "
                "consumed_at) VALUES (?, ?, ?, ?, NULL)",
                (str(uuid4()), "e" * 64, NOW, NOW),  # must expire after it was created
            )
        connection.execute(
            "INSERT INTO mobile_sessions (id, session_hash, csrf_hash, created_at, expires_at, "
            "last_seen_at, revoked_at) VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (
                str(uuid4()),
                "f" * 64,
                "0" * 64,
                NOW,
                "2026-10-19T12:00:00.000000+00:00",
                NOW,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                "INSERT INTO mobile_sessions (id, session_hash, csrf_hash, created_at, "
                "expires_at, last_seen_at, revoked_at) VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (
                    str(uuid4()),
                    "f" * 64,
                    "1" * 64,
                    NOW,
                    "2026-10-19T12:00:00.000000+00:00",
                    NOW,
                ),
            )


def test_upgrade_adds_the_inbound_mail_tables(tmp_path: Path, clock: FakeClock) -> None:
    """0007 adds durable mail and leaves every earlier row untouched."""
    database = Database.at(tmp_path / "assistant.db")
    legacy_directory = tmp_path / "legacy"
    legacy_directory.mkdir()
    shipped = default_migrations_dir()
    for name in (
        "0001_initial.sql",
        "0002_event_processing_leases.sql",
        "0003_storage_catalog.sql",
        "0004_commitment_core.sql",
        "0005_planning_proposals.sql",
        "0006_scheduler_notifications.sql",
    ):
        (legacy_directory / name).write_text(
            (shipped / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    apply_migrations(database, clock=clock, directory=legacy_directory)
    event_id = str(uuid4())
    task_id = str(uuid4())
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO inbound_events (id, source, external_id, event_type, content, "
            "status, attempts, received_at, created_at, updated_at) VALUES (?, 'cli', 'x', "
            "'local.note', 'body', 'RECEIVED', 0, ?, ?, ?)",
            (event_id, NOW, NOW, NOW),
        )
        connection.execute(
            "INSERT INTO tasks (id, title, status, priority, estimated_minutes, created_at, "
            "updated_at) VALUES (?, 'Write report', 'open', 'high', 300, ?, ?)",
            (task_id, NOW, NOW),
        )

    applied = apply_migrations(database, clock=clock)

    assert [migration.version for migration in applied] == [
        "0007", "0008", "0009", "0010", "0011", "0012",
        "0013", "0014", "0015", "0016", "0017", "0018", "0019", "0020", "0021", "0022", "0023",
    ]
    with database.connect() as connection:
        names = {
            str(row["name"])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        events = connection.execute(
            "SELECT count(*) AS total FROM inbound_events"
        ).fetchone()
        tasks = connection.execute("SELECT count(*) AS total FROM tasks").fetchone()
    assert {
        "mailbox_sync_state",
        "mail_messages",
        "mail_message_locations",
        "mail_attachments",
        "mail_event_links",
    } <= names
    assert events["total"] == 1 and tasks["total"] == 1  # older data survived

    _assert_mail_constraints(database)

    assert apply_migrations(database, clock=clock) == ()
    assert applied_versions(database) == (
        "0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009", "0010",
        "0011", "0012", "0013", "0014", "0015", "0016", "0017",
        "0018", "0019", "0020", "0021", "0022", "0023",
    )


def _assert_mail_constraints(database: Database) -> None:
    """The mail schema's identity, status and provenance rules are enforced by SQLite."""
    message_id = str(uuid4())
    stamp = NOW
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO mail_messages (id, account_id, references_json, to_addresses_json, "
            "cc_addresses_json, body_status, content_fingerprint, size_bytes, parse_warnings, "
            "first_seen_at, last_seen_at) VALUES (?, 'smail', '[]', '[]', '[]', 'available', ?, "
            "10, 0, ?, ?)",
            (message_id, "f" * 64, stamp, stamp),
        )
        # Message-ID is not unique: real mailboxes contain duplicates.
        for _ in range(2):
            connection.execute(
                "INSERT INTO mail_messages (id, account_id, message_id_header, "
                "references_json, to_addresses_json, cc_addresses_json, body_status, "
                "content_fingerprint, size_bytes, parse_warnings, first_seen_at, last_seen_at) "
                "VALUES (?, 'smail', '<dup@example.edu>', '[]', '[]', '[]', 'available', ?, 10, "
                "0, ?, ?)",
                (str(uuid4()), "e" * 64, stamp, stamp),
            )
        connection.execute(
            "INSERT INTO mail_message_locations (message_id, account_id, mailbox_name, "
            "uidvalidity, uid, first_seen_at, last_seen_at) VALUES (?, 'smail', 'INBOX', 1, 1, "
            "?, ?)",
            (message_id, stamp, stamp),
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                "INSERT INTO mail_message_locations (message_id, account_id, mailbox_name, "
                "uidvalidity, uid, first_seen_at, last_seen_at) VALUES (?, 'smail', 'INBOX', 1, "
                "1, ?, ?)",
                (message_id, stamp, stamp),
            )
        with pytest.raises(sqlite3.IntegrityError, match="body_status_is_known"):
            connection.execute(
                "INSERT INTO mail_messages (id, account_id, references_json, "
                "to_addresses_json, cc_addresses_json, body_status, content_fingerprint, "
                "size_bytes, parse_warnings, first_seen_at, last_seen_at) VALUES (?, 'smail', "
                "'[]', '[]', '[]', 'unknown', ?, 1, 0, ?, ?)",
                (str(uuid4()), "d" * 64, stamp, stamp),
            )
        with pytest.raises(sqlite3.IntegrityError, match="mailbox_sync_state_cursor_not_negative"):
            connection.execute(
                "INSERT INTO mailbox_sync_state (account_id, mailbox_name, uidvalidity, "
                "last_seen_uid, mode, updated_at) VALUES ('smail', 'BAD', 1, -1, 'normal', ?)",
                (stamp,),
            )


def test_upgrade_adds_mail_threads_and_analyses(tmp_path: Path, clock: FakeClock) -> None:
    """0008 adds threading and analyses and leaves every Phase 5A row untouched."""
    database = Database.at(tmp_path / "assistant.db")
    legacy_directory = tmp_path / "legacy"
    legacy_directory.mkdir()
    shipped = default_migrations_dir()
    for name in (
        "0001_initial.sql",
        "0002_event_processing_leases.sql",
        "0003_storage_catalog.sql",
        "0004_commitment_core.sql",
        "0005_planning_proposals.sql",
        "0006_scheduler_notifications.sql",
        "0007_inbound_mail.sql",
    ):
        (legacy_directory / name).write_text(
            (shipped / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    apply_migrations(database, clock=clock, directory=legacy_directory)
    message_id = str(uuid4())
    event_id = str(uuid4())
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO mail_messages (id, account_id, message_id_header, references_json, "
            "to_addresses_json, cc_addresses_json, body_status, content_fingerprint, "
            "size_bytes, parse_warnings, first_seen_at, last_seen_at) VALUES (?, 'smail', "
            "'<a@example.edu>', '[]', '[]', '[]', 'available', ?, 10, 0, ?, ?)",
            (message_id, "a" * 64, NOW, NOW),
        )
        connection.execute(
            "INSERT INTO inbound_events (id, source, external_id, event_type, content, "
            "status, attempts, received_at, created_at, updated_at) VALUES (?, 'mail:smail', "
            "'message:x', 'mail.message.received', '{}', 'RECEIVED', 0, ?, ?, ?)",
            (event_id, NOW, NOW, NOW),
        )

    applied = apply_migrations(database, clock=clock)

    assert [migration.version for migration in applied] == [
        "0008", "0009", "0010", "0011", "0012", "0013",
        "0014", "0015", "0016", "0017", "0018", "0019", "0020", "0021", "0022", "0023",
    ]
    with database.connect() as connection:
        names = {
            str(row["name"])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        messages = connection.execute(
            "SELECT count(*) AS total FROM mail_messages"
        ).fetchone()
        events = connection.execute(
            "SELECT count(*) AS total FROM inbound_events"
        ).fetchone()
    assert {"mail_threads", "mail_thread_members", "mail_analyses"} <= names
    assert messages["total"] == 1 and events["total"] == 1  # older data survived

    _assert_mail_intelligence_constraints(database, message_id=message_id)

    assert apply_migrations(database, clock=clock) == ()
    assert applied_versions(database) == (
        "0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009", "0010",
        "0011", "0012", "0013", "0014", "0015", "0016", "0017",
        "0018", "0019", "0020", "0021", "0022", "0023",
    )


def _assert_mail_intelligence_constraints(database: Database, *, message_id: str) -> None:
    """Thread membership and analysis rules are enforced by SQLite, not by convention."""
    thread_id = str(uuid4())
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO mail_threads (id, account_id, created_at, updated_at) "
            "VALUES (?, 'smail', ?, ?)",
            (thread_id, NOW, NOW),
        )
        connection.execute(
            "INSERT INTO mail_thread_members (message_id, thread_id, parent_message_id, "
            "link_status, linked_at) VALUES (?, ?, NULL, 'root', ?)",
            (message_id, thread_id, NOW),
        )
        with pytest.raises(sqlite3.IntegrityError, match="mail_thread_members_status_is_known"):
            connection.execute(
                "INSERT INTO mail_thread_members (message_id, thread_id, parent_message_id, "
                "link_status, linked_at) VALUES (?, ?, NULL, 'guessed', ?)",
                (str(uuid4()), thread_id, NOW),
            )
        # A member is either linked to a parent or explicitly not: never both, never neither.
        with pytest.raises(
            sqlite3.IntegrityError, match="mail_thread_members_parent_matches_status"
        ):
            connection.execute(
                "INSERT INTO mail_thread_members (message_id, thread_id, parent_message_id, "
                "link_status, linked_at) VALUES (?, ?, ?, 'ambiguous', ?)",
                (str(uuid4()), thread_id, message_id, NOW),
            )
        # The membership is keyed by message, so one message belongs to exactly one thread.
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                "INSERT INTO mail_thread_members (message_id, thread_id, parent_message_id, "
                "link_status, linked_at) VALUES (?, ?, NULL, 'root', ?)",
                (message_id, thread_id, NOW),
            )
        connection.execute(
            "INSERT INTO mail_analyses (message_id, analyzer_version, input_fingerprint, "
            "category, requires_reply, summary, action_candidates_json, created_at, updated_at) "
            "VALUES (?, 1, ?, 'actionable_notice', 1, 'Registration closes soon', '[]', ?, ?)",
            (message_id, "b" * 64, NOW, NOW),
        )
        with pytest.raises(sqlite3.IntegrityError, match="mail_analyses_category_is_known"):
            connection.execute(
                "INSERT INTO mail_analyses (message_id, analyzer_version, input_fingerprint, "
                "category, requires_reply, summary, action_candidates_json, created_at, "
                "updated_at) VALUES (?, 1, ?, 'confident', 0, 'x', '[]', ?, ?)",
                (str(uuid4()), "c" * 64, NOW, NOW),
            )
        # Candidates are stored as a JSON array, never as an object or a bare string.
        with pytest.raises(sqlite3.IntegrityError, match="mail_analyses_candidates_is_json"):
            connection.execute(
                "INSERT INTO mail_analyses (message_id, analyzer_version, input_fingerprint, "
                "category, requires_reply, summary, action_candidates_json, created_at, "
                "updated_at) VALUES (?, 1, ?, 'unknown', 0, 'x', '{}', ?, ?)",
                (str(uuid4()), "d" * 64, NOW, NOW),
            )
        # An analysis can only exist for a stored message.
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                "INSERT INTO mail_analyses (message_id, analyzer_version, input_fingerprint, "
                "category, requires_reply, summary, action_candidates_json, created_at, "
                "updated_at) VALUES (?, 1, ?, 'unknown', 0, 'x', '[]', ?, ?)",
                (str(uuid4()), "e" * 64, NOW, NOW),
            )


def test_upgrade_adds_reply_drafts_and_the_reply_to_column(
    tmp_path: Path, clock: FakeClock
) -> None:
    """0009 adds durable drafts and a Reply-To column without rewriting a single stored row."""
    database = Database.at(tmp_path / "assistant.db")
    legacy_directory = tmp_path / "legacy"
    legacy_directory.mkdir()
    shipped = default_migrations_dir()
    for name in (
        "0001_initial.sql",
        "0002_event_processing_leases.sql",
        "0003_storage_catalog.sql",
        "0004_commitment_core.sql",
        "0005_planning_proposals.sql",
        "0006_scheduler_notifications.sql",
        "0007_inbound_mail.sql",
        "0008_mail_intelligence.sql",
    ):
        (legacy_directory / name).write_text(
            (shipped / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    apply_migrations(database, clock=clock, directory=legacy_directory)
    message_id = str(uuid4())
    thread_id = str(uuid4())
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO mail_messages (id, account_id, message_id_header, references_json, "
            "to_addresses_json, cc_addresses_json, body_status, content_fingerprint, "
            "size_bytes, parse_warnings, first_seen_at, last_seen_at) VALUES (?, 'smail', "
            "'<a@example.edu>', '[]', '[]', '[]', 'available', ?, 10, 0, ?, ?)",
            (message_id, "a" * 64, NOW, NOW),
        )
        connection.execute(
            "INSERT INTO mail_threads (id, account_id, created_at, updated_at) "
            "VALUES (?, 'smail', ?, ?)",
            (thread_id, NOW, NOW),
        )
        connection.execute(
            "INSERT INTO mail_thread_members (message_id, thread_id, parent_message_id, "
            "link_status, linked_at) VALUES (?, ?, NULL, 'root', ?)",
            (message_id, thread_id, NOW),
        )

    applied = apply_migrations(database, clock=clock)

    assert [migration.version for migration in applied] == [
        "0009", "0010", "0011", "0012", "0013", "0014",
        "0015", "0016", "0017", "0018", "0019", "0020", "0021", "0022", "0023",
    ]
    with database.connect() as connection:
        names = {
            str(row["name"])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        stored = connection.execute(
            "SELECT reply_to_addresses_json, body_status, content_fingerprint "
            "FROM mail_messages WHERE id = ?",
            (message_id,),
        ).fetchone()
        threads = connection.execute(
            "SELECT count(*) AS total FROM mail_threads"
        ).fetchone()
    assert {"mail_drafts", "mail_draft_sources"} <= names
    assert stored["reply_to_addresses_json"] == "[]"  # the new column defaults, nothing rewrites
    assert stored["body_status"] == "available"
    assert stored["content_fingerprint"] == "a" * 64
    assert threads["total"] == 1  # Phase 5B rows survived

    _assert_mail_draft_constraints(database, message_id=message_id, thread_id=thread_id)

    assert apply_migrations(database, clock=clock) == ()
    assert applied_versions(database) == (
        "0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009", "0010",
        "0011", "0012", "0013", "0014", "0015", "0016", "0017",
        "0018", "0019", "0020", "0021", "0022", "0023",
    )


def _assert_mail_draft_constraints(
    database: Database, *, message_id: str, thread_id: str
) -> None:
    """Draft identity, status and provenance rules are enforced by SQLite."""
    draft_id = str(uuid4())
    insert = (
        "INSERT INTO mail_drafts (id, account_id, thread_id, reply_to_message_id, "
        "to_addresses_json, subject, body_text, needs_user_input_json, origin, version, "
        "generation_input_fingerprint, prompt_version, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )

    def values(**overrides: object) -> tuple[object, ...]:
        base: dict[str, object] = {
            "id": draft_id,
            "account": "smail",
            "thread": thread_id,
            "message": message_id,
            "to": '["ada@example.edu"]',
            "subject": "Re: SE lab",
            "body": "A local draft.",
            "needs": "[]",
            "origin": "model_generated",
            "version": 1,
            "fingerprint": "f" * 64,
            "prompt_version": 1,
            "created": NOW,
            "updated": NOW,
        }
        base.update(overrides)
        return (
            base["id"],
            base["account"],
            base["thread"],
            base["message"],
            base["to"],
            base["subject"],
            base["body"],
            base["needs"],
            base["origin"],
            base["version"],
            base["fingerprint"],
            base["prompt_version"],
            base["created"],
            base["updated"],
        )

    with database.connect() as connection:
        connection.execute(insert, values())
        connection.execute(
            "INSERT INTO mail_draft_sources (draft_id, ordinal, root_id, entry_id, chunk_id, "
            "logical_uri, source_span_json) VALUES (?, 0, 'university', ?, ?, "
            "'vault://university/notes/hours.md', '{\"kind\":\"line\",\"line_start\":18,"
            "\"line_end\":31}')",
            (draft_id, str(uuid4()), str(uuid4())),
        )
        for overrides, expected in (
            ({"origin": "sent"}, "mail_drafts_origin_is_known"),
            ({"version": 0}, "mail_drafts_version_positive"),
            ({"subject": "   "}, "mail_drafts_subject_not_blank"),
            ({"body": "  "}, "mail_drafts_body_not_blank"),
            ({"to": "{}"}, "mail_drafts_recipients_is_json"),
            ({"needs": '"x"'}, "mail_drafts_input_is_json"),
            ({"fingerprint": "short"}, "mail_drafts_fingerprint_is_sha256"),
            ({"message": str(uuid4())}, "FOREIGN KEY"),
            ({"thread": str(uuid4())}, "FOREIGN KEY"),
        ):
            with pytest.raises(sqlite3.IntegrityError, match=expected):
                # A fresh id per case, so an identity collision cannot mask the rule under test.
                connection.execute(insert, values(**{"id": str(uuid4()), **overrides}))
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(insert, values())
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                "INSERT INTO mail_draft_sources (draft_id, ordinal, root_id, entry_id, "
                "chunk_id, logical_uri, source_span_json) VALUES (?, 0, 'university', ?, ?, "
                "'vault://university/notes/hours.md', '{\"kind\":\"page\",\"page_number\":1}')",
                (draft_id, str(uuid4()), str(uuid4())),
            )
        # A draft may exist without a thread and without any knowledge source.
        connection.execute(
            insert,
            values(id=str(uuid4()), thread=None, needs="[]"),
        )


def test_upgrade_adds_the_case_action_approval_tables(
    tmp_path: Path, clock: FakeClock
) -> None:
    """0010 adds the action boundary and leaves every earlier row untouched."""
    database = Database.at(tmp_path / "assistant.db")
    legacy_directory = tmp_path / "legacy"
    legacy_directory.mkdir()
    shipped = default_migrations_dir()
    for name in (
        "0001_initial.sql",
        "0002_event_processing_leases.sql",
        "0003_storage_catalog.sql",
        "0004_commitment_core.sql",
        "0005_planning_proposals.sql",
        "0006_scheduler_notifications.sql",
        "0007_inbound_mail.sql",
        "0008_mail_intelligence.sql",
        "0009_mail_reply_drafts.sql",
    ):
        (legacy_directory / name).write_text(
            (shipped / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    apply_migrations(database, clock=clock, directory=legacy_directory)
    message_id = str(uuid4())
    task_id = str(uuid4())
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO mail_messages (id, account_id, message_id_header, references_json, "
            "to_addresses_json, cc_addresses_json, reply_to_addresses_json, body_status, "
            "content_fingerprint, size_bytes, parse_warnings, first_seen_at, last_seen_at) "
            "VALUES (?, 'smail', '<a@example.edu>', '[]', '[]', '[]', '[]', 'available', ?, "
            "10, 0, ?, ?)",
            (message_id, "a" * 64, NOW, NOW),
        )
        connection.execute(
            "INSERT INTO tasks (id, title, status, priority, estimated_minutes, created_at, "
            "updated_at) VALUES (?, 'Write report', 'open', 'high', 300, ?, ?)",
            (task_id, NOW, NOW),
        )

    applied = apply_migrations(database, clock=clock)

    assert [migration.version for migration in applied] == [
        "0010", "0011", "0012", "0013", "0014",
        "0015", "0016", "0017", "0018", "0019", "0020", "0021", "0022", "0023",
    ]
    with database.connect() as connection:
        names = {
            str(row["name"])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        messages = connection.execute(
            "SELECT count(*) AS total FROM mail_messages"
        ).fetchone()
        tasks = connection.execute("SELECT count(*) AS total FROM tasks").fetchone()
    assert {
        "cases",
        "action_requests",
        "approval_challenges",
        "approvals",
        "execution_runs",
    } <= names
    assert messages["total"] == 1 and tasks["total"] == 1  # older data survived

    _assert_action_constraints(database)

    assert apply_migrations(database, clock=clock) == ()
    assert applied_versions(database) == (
        "0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009", "0010",
        "0011", "0012", "0013", "0014", "0015", "0016", "0017",
        "0018", "0019", "0020", "0021", "0022", "0023",
    )


def _assert_action_constraints(database: Database) -> None:
    """The action boundary's rules are enforced by SQLite, not only by the services."""
    case_id = str(uuid4())
    action_id = str(uuid4())
    challenge_id = str(uuid4())
    approval_id = str(uuid4())
    insert_case = (
        "INSERT INTO cases (id, title, status, created_at, updated_at, completed_at, "
        "cancelled_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
    )
    insert_action = (
        "INSERT INTO action_requests (id, case_id, action_type, payload_json, fingerprint, "
        "status, created_at, executed_at, cancelled_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    insert_challenge = (
        "INSERT INTO approval_challenges (id, action_id, action_fingerprint, token_hash, "
        "created_at, expires_at, consumed_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
    )
    insert_approval = (
        "INSERT INTO approvals (id, action_id, action_fingerprint, approved_at, expires_at, "
        "consumed_at, superseded_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
    )
    later = "2026-09-23T09:10:00.000000+00:00"
    with database.connect() as connection:
        connection.execute(
            insert_case, (case_id, "Register", "open", NOW, NOW, None, None)
        )
        connection.execute(
            insert_action,
            (
                action_id,
                case_id,
                "mail.send",
                '{"body":"hello"}',
                "a" * 64,
                "prepared",
                NOW,
                None,
                None,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="action_requests_status_is_known"):
            connection.execute(
                insert_action,
                (
                    str(uuid4()),
                    case_id,
                    "mail.send",
                    "{}",
                    "a" * 64,
                    "sent",
                    NOW,
                    None,
                    None,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="action_requests_type_is_namespaced"):
            connection.execute(
                insert_action,
                (
                    str(uuid4()),
                    case_id,
                    "send",
                    "{}",
                    "a" * 64,
                    "prepared",
                    NOW,
                    None,
                    None,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="action_requests_payload_is_json"):
            connection.execute(
                insert_action,
                (
                    str(uuid4()),
                    case_id,
                    "mail.send",
                    "not json",
                    "a" * 64,
                    "prepared",
                    NOW,
                    None,
                    None,
                ),
            )
        # An action belongs to a case that exists.
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                insert_action,
                (
                    str(uuid4()),
                    str(uuid4()),
                    "mail.send",
                    "{}",
                    "a" * 64,
                    "prepared",
                    NOW,
                    None,
                    None,
                ),
            )
        # A challenge always expires after it was created, and its token hash is a hash.
        connection.execute(
            insert_challenge,
            (challenge_id, action_id, "a" * 64, "b" * 64, NOW, later, None),
        )
        with pytest.raises(sqlite3.IntegrityError, match="expires_after_creation"):
            connection.execute(
                insert_challenge,
                (str(uuid4()), action_id, "a" * 64, "c" * 64, later, NOW, None),
            )
        with pytest.raises(sqlite3.IntegrityError, match="token_hash_is_sha256"):
            connection.execute(
                insert_challenge,
                (str(uuid4()), action_id, "a" * 64, "SECRET", NOW, later, None),
            )
        # Two challenges can never share a token hash.
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                insert_challenge,
                (str(uuid4()), action_id, "a" * 64, "b" * 64, NOW, later, None),
            )
        # At most one live approval per action.
        connection.execute(
            insert_approval,
            (approval_id, action_id, "a" * 64, NOW, later, None, None),
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                insert_approval,
                (str(uuid4()), action_id, "a" * 64, NOW, later, None, None),
            )
        # Consuming the first makes room for a later, explicit approval.
        connection.execute(
            "UPDATE approvals SET consumed_at = ? WHERE id = ?", (NOW, approval_id)
        )
        connection.execute(
            insert_approval,
            (str(uuid4()), action_id, "a" * 64, NOW, later, None, None),
        )
        # A run is finished exactly when its status says so.
        with pytest.raises(sqlite3.IntegrityError, match="execution_runs_status_is_known"):
            connection.execute(
                "INSERT INTO execution_runs (id, action_id, approval_id, status, started_at, "
                "finished_at, error_summary) VALUES (?, ?, ?, 'sent', ?, NULL, NULL)",
                (str(uuid4()), action_id, approval_id, NOW),
            )
        with pytest.raises(sqlite3.IntegrityError, match="finished_matches_status"):
            connection.execute(
                "INSERT INTO execution_runs (id, action_id, approval_id, status, started_at, "
                "finished_at, error_summary) VALUES (?, ?, ?, 'succeeded', ?, NULL, NULL)",
                (str(uuid4()), action_id, approval_id, NOW),
            )


def test_upgrade_adds_send_links_and_reconciliations(tmp_path: Path, clock: FakeClock) -> None:
    """0011 adds the approved-send tables and a draft column, without rewriting old rows."""
    database = Database.at(tmp_path / "assistant.db")
    legacy_directory = tmp_path / "legacy"
    legacy_directory.mkdir()
    shipped = default_migrations_dir()
    for name in (
        "0001_initial.sql",
        "0002_event_processing_leases.sql",
        "0003_storage_catalog.sql",
        "0004_commitment_core.sql",
        "0005_planning_proposals.sql",
        "0006_scheduler_notifications.sql",
        "0007_inbound_mail.sql",
        "0008_mail_intelligence.sql",
        "0009_mail_reply_drafts.sql",
        "0010_case_action_approval.sql",
    ):
        (legacy_directory / name).write_text(
            (shipped / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    apply_migrations(database, clock=clock, directory=legacy_directory)
    message_id = str(uuid4())
    draft_id = str(uuid4())
    case_id = str(uuid4())
    action_id = str(uuid4())
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO mail_messages (id, account_id, message_id_header, references_json, "
            "to_addresses_json, cc_addresses_json, reply_to_addresses_json, body_status, "
            "content_fingerprint, size_bytes, parse_warnings, first_seen_at, last_seen_at) "
            "VALUES (?, 'smail', '<a@example.edu>', '[]', '[]', '[]', '[]', 'available', ?, "
            "10, 0, ?, ?)",
            (message_id, "a" * 64, NOW, NOW),
        )
    draft_id = str(uuid4())
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO mail_drafts (id, account_id, reply_to_message_id, to_addresses_json, "
            "subject, body_text, needs_user_input_json, origin, version, "
            "generation_input_fingerprint, prompt_version, created_at, updated_at) "
            "VALUES (?, 'smail', ?, '[\"ada@example.edu\"]', 'Re: x', 'body', '[]', "
            "'model_generated', 1, ?, 1, ?, ?)",
            (draft_id, message_id, "b" * 64, NOW, NOW),
        )
        connection.execute(
            "INSERT INTO cases (id, title, status, created_at, updated_at, completed_at, "
            "cancelled_at) VALUES (?, 'Send it', 'open', ?, ?, NULL, NULL)",
            (case_id, NOW, NOW),
        )
        connection.execute(
            "INSERT INTO action_requests (id, case_id, action_type, payload_json, fingerprint, "
            "status, created_at, executed_at, cancelled_at) VALUES (?, ?, 'mail.send', ?, ?, "
            "'prepared', ?, NULL, NULL)",
            (action_id, case_id, '{"body":"x"}', "c" * 64, NOW),
        )
        approvals_row_id = str(uuid4())
        connection.execute(
            "INSERT INTO approvals (id, action_id, action_fingerprint, approved_at, "
            "expires_at, consumed_at, superseded_at) VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (
                approvals_row_id,
                action_id,
                "c" * 64,
                NOW,
                "2026-09-23T09:00:00.000000+00:00",
                "2026-09-22T09:05:00.000000+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO execution_runs (id, action_id, approval_id, status, started_at, "
            "finished_at, error_summary) VALUES (?, ?, ?, 'unknown', ?, ?, 'no response')",
            (str(uuid4()), action_id, approvals_row_id, NOW, NOW),
        )

    applied = apply_migrations(database, clock=clock)

    assert [migration.version for migration in applied] == [
        "0011", "0012", "0013", "0014", "0015", "0016", "0017",
        "0018", "0019", "0020", "0021", "0022", "0023",
    ]
    with database.connect() as connection:
        names = {
            str(row["name"])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        acknowledged = connection.execute(
            "SELECT needs_user_input_acknowledged_at FROM mail_drafts WHERE id = ?",
            (draft_id,),
        ).fetchone()
        drafts = connection.execute("SELECT count(*) AS total FROM mail_drafts").fetchone()
        actions = connection.execute(
            "SELECT count(*) AS total FROM action_requests"
        ).fetchone()
    assert {"mail_send_links", "mail_send_reconciliations"} <= names
    assert acknowledged["needs_user_input_acknowledged_at"] is None  # defaults to unacknowledged
    assert drafts["total"] == 1 and actions["total"] == 1  # older data survived

    _assert_mail_send_constraints(database, draft_id=draft_id, action_id=action_id)

    assert apply_migrations(database, clock=clock) == ()
    assert applied_versions(database) == (
        "0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009", "0010",
        "0011", "0012", "0013", "0014", "0015", "0016", "0017",
        "0018", "0019", "0020", "0021", "0022", "0023",
    )


def _assert_mail_send_constraints(
    database: Database, *, draft_id: str, action_id: str
) -> None:
    """The send tables enforce one-version-one-action, one-Message-ID and located FOUND rows."""
    insert_link = (
        "INSERT INTO mail_send_links (action_id, draft_id, draft_version, rfc_message_id, "
        "created_at) VALUES (?, ?, ?, ?, ?)"
    )
    with database.connect() as connection:
        connection.execute(
            insert_link, (action_id, draft_id, 1, "<abc@example.edu>", NOW)
        )
        # One draft version can never produce a second send action.
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                insert_link, (str(uuid4()), draft_id, 1, "<other@example.edu>", NOW)
            )
        # Neither can one Message-ID.
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                insert_link, (str(uuid4()), draft_id, 2, "<abc@example.edu>", NOW)
            )
        with pytest.raises(sqlite3.IntegrityError, match="mail_send_links_version_positive"):
            connection.execute(
                insert_link, (str(uuid4()), draft_id, 0, "<v@example.edu>", NOW)
            )
        # A link always names a real action.
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                insert_link, (str(uuid4()), draft_id, 3, "<fk@example.edu>", NOW)
            )

        run_row = connection.execute(
            "SELECT id FROM execution_runs WHERE action_id = ?", (action_id,)
        ).fetchone()
        insert_reconciliation = (
            "INSERT INTO mail_send_reconciliations (id, action_id, execution_run_id, result, "
            "checked_at, mailbox_name, uidvalidity, uid) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        )
        connection.execute(
            insert_reconciliation,
            (str(uuid4()), action_id, str(run_row["id"]), "not_found", NOW, None, None, None),
        )
        with pytest.raises(sqlite3.IntegrityError, match="result_is_known"):
            connection.execute(
                insert_reconciliation,
                (str(uuid4()), action_id, str(run_row["id"]), "maybe", NOW, None, None, None),
            )
        with pytest.raises(sqlite3.IntegrityError, match="found_has_location"):
            connection.execute(
                insert_reconciliation,
                (str(uuid4()), action_id, str(run_row["id"]), "found", NOW, None, None, None),
            )
        connection.execute(
            insert_reconciliation,
            (
                str(uuid4()),
                action_id,
                str(run_row["id"]),
                "found",
                NOW,
                "Sent",
                7,
                42,
            ),
        )
        # History is append-only: both rows are still there, oldest first.
        stored = connection.execute(
            "SELECT result FROM mail_send_reconciliations WHERE action_id = ? "
            "ORDER BY checked_at, rowid",
            (action_id,),
        ).fetchall()
    assert [str(row["result"]) for row in stored] == ["not_found", "found"]


def test_upgrade_adds_scheduled_jobs_and_notifications(
    tmp_path: Path, clock: FakeClock
) -> None:
    """0006 adds the scheduler tables and leaves every Phase 3B row untouched."""
    database = Database.at(tmp_path / "assistant.db")
    legacy_directory = tmp_path / "legacy"
    legacy_directory.mkdir()
    shipped = default_migrations_dir()
    for name in (
        "0001_initial.sql",
        "0002_event_processing_leases.sql",
        "0003_storage_catalog.sql",
        "0004_commitment_core.sql",
        "0005_planning_proposals.sql",
    ):
        (legacy_directory / name).write_text(
            (shipped / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    apply_migrations(database, clock=clock, directory=legacy_directory)
    task_id = str(uuid4())
    block_id = str(uuid4())
    proposal_id = str(uuid4())
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO tasks (id, title, status, priority, estimated_minutes, created_at, "
            "updated_at) VALUES (?, 'Write report', 'open', 'high', 300, ?, ?)",
            (task_id, NOW, NOW),
        )
        connection.execute(
            "INSERT INTO plan_blocks (id, task_id, starts_at, ends_at, created_at, updated_at, "
            "origin, proposal_id) VALUES (?, ?, ?, '2026-09-20T11:00:00.000000+00:00', ?, ?, "
            "'manual', NULL)",
            (block_id, task_id, NOW, NOW, NOW),
        )
        connection.execute(
            "INSERT INTO plan_proposals (id, status, window_start, window_end, timezone, "
            "input_fingerprint, input_revision, created_at) VALUES (?, 'pending', ?, ?, "
            "'Asia/Shanghai', ?, 0, ?)",
            (
                proposal_id,
                NOW,
                "2026-09-27T16:00:00.000000+00:00",
                "f" * 64,
                NOW,
            ),
        )

    applied = apply_migrations(database, clock=clock)

    assert [migration.version for migration in applied] == [
        "0006", "0007", "0008", "0009", "0010", "0011",
        "0012", "0013", "0014", "0015", "0016", "0017", "0018", "0019", "0020", "0021",
        "0022", "0023",
    ]
    with database.connect() as connection:
        tables = {
            str(row["name"])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        revision = connection.execute(
            "SELECT value FROM commitment_meta WHERE key = 'revision'"
        ).fetchone()
        kept = connection.execute(
            "SELECT status, window_end FROM plan_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        block = connection.execute(
            "SELECT origin FROM plan_blocks WHERE id = ?", (block_id,)
        ).fetchone()

    assert {"scheduled_jobs", "notifications"} <= tables
    assert revision is not None and revision["value"] == "0"  # untouched baseline
    assert kept is not None and kept["status"] == "pending"
    assert block is not None and block["origin"] == "manual"

    _assert_scheduler_constraints(database)

    assert apply_migrations(database, clock=clock) == ()
    assert applied_versions(database) == (
        "0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009", "0010",
        "0011", "0012", "0013", "0014", "0015", "0016", "0017",
        "0018", "0019", "0020", "0021", "0022", "0023",
    )


def _assert_scheduler_constraints(database: Database) -> None:
    """The active-dedup index is partial; the notification key is unique."""
    stamp = "2026-09-20T12:00:00.000000+00:00"
    cancelled = (
        "INSERT INTO scheduled_jobs (id, kind, status, due_at, dedup_key, payload_json, "
        "attempts, created_at, updated_at, cancelled_at) VALUES (?, 'rolling_replan', "
        "'cancelled', ?, ?, '{}', 0, ?, ?, ?)"
    )
    pending = (
        "INSERT INTO scheduled_jobs (id, kind, status, due_at, dedup_key, payload_json, "
        "attempts, created_at, updated_at) VALUES (?, 'rolling_replan', 'pending', ?, ?, "
        "'{}', 0, ?, ?)"
    )
    with database.connect() as connection:
        # Terminal jobs may share a dedup key: history is allowed to repeat.
        connection.execute(cancelled, (str(uuid4()), stamp, "dup", stamp, stamp, stamp))
        connection.execute(cancelled, (str(uuid4()), stamp, "dup", stamp, stamp, stamp))
        # Active jobs may not.
        connection.execute(pending, (str(uuid4()), stamp, "live", stamp, stamp))
        with pytest.raises(sqlite3.IntegrityError, match=r"scheduled_jobs\.dedup_key"):
            connection.execute(pending, (str(uuid4()), stamp, "live", stamp, stamp))

        notifications = (
            "INSERT INTO notifications (id, kind, status, title, body, dedup_key, created_at) "
            "VALUES (?, 'plan_ready', 'unread', 't', 'b', ?, ?)"
        )
        connection.execute(notifications, (str(uuid4()), "same", stamp))
        with pytest.raises(sqlite3.IntegrityError, match="dedup_key"):
            connection.execute(notifications, (str(uuid4()), "same", stamp))


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

    assert [migration.version for migration in applied] == [
        "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009",
        "0010", "0011", "0012", "0013", "0014", "0015", "0016",
        "0017", "0018", "0019", "0020", "0021", "0022", "0023",
    ]
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
    assert applied_versions(database) == (
        "0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009", "0010",
        "0011", "0012", "0013", "0014", "0015", "0016", "0017",
        "0018", "0019", "0020", "0021", "0022", "0023",
    )


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

    assert [migration.version for migration in applied] == [
        "0003", "0004", "0005", "0006", "0007", "0008", "0009", "0010", "0011", "0012",
        "0013", "0014", "0015", "0016", "0017", "0018", "0019", "0020", "0021", "0022", "0023",
    ]
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
    assert applied_versions(database) == (
        "0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009", "0010",
        "0011", "0012", "0013", "0014", "0015", "0016", "0017",
        "0018", "0019", "0020", "0021", "0022", "0023",
    )


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

    assert [migration.version for migration in applied] == [
        "0004", "0005", "0006", "0007", "0008", "0009", "0010", "0011", "0012",
        "0013", "0014", "0015", "0016", "0017", "0018", "0019", "0020", "0021", "0022", "0023",
    ]
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
    assert applied_versions(database) == (
        "0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009", "0010",
        "0011", "0012", "0013", "0014", "0015", "0016", "0017",
        "0018", "0019", "0020", "0021", "0022", "0023",
    )


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

    assert [migration.version for migration in applied] == [
        "0005", "0006", "0007", "0008", "0009", "0010", "0011", "0012", "0013",
        "0014", "0015", "0016", "0017", "0018", "0019", "0020", "0021", "0022", "0023",
    ]
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
    assert applied_versions(database) == (
        "0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009", "0010",
        "0011", "0012", "0013", "0014", "0015", "0016", "0017",
        "0018", "0019", "0020", "0021", "0022", "0023",
    )


def test_upgrade_widens_the_external_review_action_type_and_keeps_every_mail_row(
    tmp_path: Path, clock: FakeClock
) -> None:
    """0023 widens one closed CHECK to a closed pair, and copies every historical row across.

    The reviewed-action table was mail-only by construction (0017). Phase 11E reaches the existing
    certificate capability through the same review, so the constraint becomes a closed pair — never
    arbitrary text — and a v1.2 database keeps its mail reviews exactly as they were.
    """
    database = Database.at(tmp_path / "assistant.db")
    legacy_directory = tmp_path / "legacy"
    legacy_directory.mkdir()
    shipped = default_migrations_dir()
    for index in range(1, 23):
        name = next(path.name for path in sorted(shipped.glob(f"{index:04d}_*.sql")))
        (legacy_directory / name).write_text(
            (shipped / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    apply_migrations(database, clock=clock, directory=legacy_directory)

    thread_id = str(uuid4())
    message_id = str(uuid4())
    turn_id = str(uuid4())
    operation_id = str(uuid4())
    case_id = str(uuid4())
    action_id = str(uuid4())
    review_id = str(uuid4())
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO conversation_threads (id, title, status, created_at, updated_at, "
            "archived_at) VALUES (?, 'Sprint', 'active', ?, ?, NULL)",
            (thread_id, NOW, NOW),
        )
        connection.execute(
            "INSERT INTO conversation_messages (id, thread_id, role, text, created_at) "
            "VALUES (?, ?, 'user', 'send it', ?)",
            (message_id, thread_id, NOW),
        )
        connection.execute(
            "INSERT INTO conversation_turns (id, thread_id, user_message_id, "
            "assistant_message_id, interpreter_version, context_fingerprint, status, created_at, "
            "completed_at) VALUES (?, ?, ?, NULL, 'v1', ?, 'planned', ?, NULL)",
            (turn_id, thread_id, message_id, "f" * 64, NOW),
        )
        connection.execute(
            "INSERT INTO conversation_operations (id, turn_id, ordinal, operation_type, "
            "arguments_json, operation_fingerprint, status, result_kind, result_ref, "
            "confirmation_expires_at, created_at, updated_at) "
            "VALUES (?, ?, 0, 'mail.prepare_new_send', '{}', ?, 'applied', 'mail_prepared', "
            "'a', NULL, ?, ?)",
            (operation_id, turn_id, "e" * 64, NOW, NOW),
        )
        connection.execute(
            "INSERT INTO cases (id, title, status, created_at, updated_at, completed_at, "
            "cancelled_at) VALUES (?, 'Send it', 'open', ?, ?, NULL, NULL)",
            (case_id, NOW, NOW),
        )
        connection.execute(
            "INSERT INTO action_requests (id, case_id, action_type, payload_json, fingerprint, "
            "status, created_at, executed_at, cancelled_at) VALUES (?, ?, 'mail.send', ?, ?, "
            "'prepared', ?, NULL, NULL)",
            (action_id, case_id, '{"body":"x"}', "a" * 64, NOW),
        )
        connection.execute(
            "INSERT INTO conversation_external_reviews (id, conversation_operation_id, thread_id,"
            " action_request_id, action_type, action_fingerprint, status, expires_at, "
            "execution_run_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'mail.send', ?, 'waiting', ?, NULL, ?, ?)",
            (
                review_id,
                operation_id,
                thread_id,
                action_id,
                "a" * 64,
                "2026-09-21T00:30:00.000000+00:00",
                NOW,
                NOW,
            ),
        )

    assert [migration.version for migration in apply_migrations(database, clock=clock)][-1] == (
        "0023"
    )

    with database.connect() as connection:
        preserved = connection.execute(
            "SELECT action_type, action_fingerprint, status, expires_at, execution_run_id "
            "FROM conversation_external_reviews WHERE id = ?",
            (review_id,),
        ).fetchone()
        assert preserved is not None
        assert tuple(preserved) == (
            "mail.send",
            "a" * 64,
            "waiting",
            "2026-09-21T00:30:00.000000+00:00",
            None,
        )
        # The widened closed set accepts the certificate capability …
        second_operation_id = str(uuid4())
        connection.execute(
            "INSERT INTO conversation_operations (id, turn_id, ordinal, operation_type, "
            "arguments_json, operation_fingerprint, status, result_kind, result_ref, "
            "confirmation_expires_at, created_at, updated_at) "
            "VALUES (?, ?, 1, 'ehall.certificate.prepare', '{}', ?, 'applied', 'ehall_prepared', "
            "'a', NULL, ?, ?)",
            (second_operation_id, turn_id, "d" * 64, NOW, NOW),
        )
        connection.execute(
            "INSERT INTO conversation_external_reviews (id, conversation_operation_id, thread_id,"
            " action_request_id, action_type, action_fingerprint, status, expires_at, "
            "execution_run_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'ehall.submit-certificate', ?, 'cancelled', ?, NULL, ?, ?)",
            (
                str(uuid4()),
                second_operation_id,
                thread_id,
                action_id,
                "b" * 64,
                "2026-09-21T00:30:00.000000+00:00",
                NOW,
                NOW,
            ),
        )
        # … and refuses anything else, because the set stays closed.
        with pytest.raises(sqlite3.IntegrityError, match="action_type_is_reviewed"):
            connection.execute(
                "INSERT INTO conversation_external_reviews (id, conversation_operation_id, "
                "thread_id, action_request_id, action_type, action_fingerprint, status, expires_at,"
                " execution_run_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'ehall.drop-course', ?, 'waiting', ?, NULL, ?, ?)",
                (
                    str(uuid4()),
                    second_operation_id,
                    thread_id,
                    action_id,
                    "c" * 64,
                    "2026-09-21T00:30:00.000000+00:00",
                    NOW,
                    NOW,
                ),
            )

    assert apply_migrations(database, clock=clock) == ()
