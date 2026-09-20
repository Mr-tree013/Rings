"""Building a runtime at a historical migration prefix, for upgrade tests (ADR-0032 §21/§49).

`prefix_directory` copies the reviewed migrations up to a version, `database_at_prefix` applies
exactly those, and `seed_era` writes rows of that era with raw SQL — the only way to produce a
database that *looks* like one an older build would have left behind. Everything here uses the real
runner; nothing fakes a schema.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from assistant.store.db import Database
from assistant.store.migrations import applied_versions, apply_migrations, discover_migrations
from tests.support.fakes import FakeClock

REPOSITORY_MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
ALL_VERSIONS = tuple(item.version for item in discover_migrations(REPOSITORY_MIGRATIONS))
REPRESENTATIVE_PREFIXES = ("0001", "0003", "0006", "0009", "0012", "0015")

STAMP = "2026-09-26T09:00:00.000000+00:00"
DIGEST = "a" * 64


def seeded_id(name: str) -> str:
    """A stable UUID for a seeded row.

    The application parses entity ids as UUIDs, so a historical fixture has to use a UUID too — a
    readable id like `task-era-0006` would make the upgraded database unreadable for a reason that
    has nothing to do with the upgrade.
    """
    return str(uuid5(NAMESPACE_URL, f"growing-assistant/upgrade-fixture/{name}"))


def prefix_directory(tmp_path: Path, prefix: str) -> Path:
    """A migrations directory holding only the reviewed files up to and including `prefix`."""
    directory = tmp_path / f"migrations-{prefix}"
    directory.mkdir(parents=True, exist_ok=True)
    for item in discover_migrations(REPOSITORY_MIGRATIONS):
        if item.version <= prefix:
            shutil.copy(item.path, directory / item.name)
    return directory


def database_at_prefix(tmp_path: Path, prefix: str, clock: FakeClock) -> Database:
    """One real database, migrated with exactly the migrations of `prefix`'s era."""
    database = Database.at(tmp_path / f"runtime-{prefix}" / "assistant.db")
    apply_migrations(database, clock=clock, directory=prefix_directory(tmp_path, prefix))
    assert applied_versions(database) == tuple(
        version for version in ALL_VERSIONS if version <= prefix
    )
    return database


def insert(connection: sqlite3.Connection, table: str, **columns: object) -> None:
    """Insert one row with explicit columns, so a seed never depends on column order."""
    names = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    connection.execute(
        f"INSERT INTO {table} ({names}) VALUES ({placeholders})", tuple(columns.values())
    )


def seed_era(connection: sqlite3.Connection, prefix: str) -> dict[str, str]:
    """Rows of one era. Returns the `{table: id}` pairs that must survive an upgrade."""
    seeded: dict[str, str] = {}
    if prefix == "0001":
        event_id = seeded_id("event-era-0001")
        insert(
            connection,
            "inbound_events",
            id=event_id,
            source="manual:manual",
            external_id=None,
            event_type="manual.input.received",
            content='{"manual_input_id":"0001"}',
            received_at=STAMP,
            status="RECEIVED",
            attempts=0,
            last_error=None,
            created_at=STAMP,
            updated_at=STAMP,
        )
        seeded["inbound_events"] = event_id
    elif prefix == "0003":
        insert(
            connection,
            "storage_roots",
            root_id="era-root",
            kind="local",
            label="Era documents",
            last_known_path="/home/user/Documents",
            first_seen_at=STAMP,
            last_seen_at=STAMP,
            last_scanned_at=None,
        )
        seeded["storage_roots"] = "era-root"
    elif prefix == "0006":
        task_id = seeded_id("task-era-0006")
        insert(
            connection,
            "tasks",
            id=task_id,
            title="An old task",
            description=None,
            status="open",
            priority="normal",
            estimated_minutes=60,
            created_at=STAMP,
            updated_at=STAMP,
            completed_at=None,
            cancelled_at=None,
        )
        insert(
            connection,
            "deadlines",
            id=seeded_id("deadline-era-0006"),
            task_id=task_id,
            due_at="2026-10-01T09:00:00.000000+00:00",
            created_at=STAMP,
            updated_at=STAMP,
        )
        insert(
            connection,
            "scheduled_jobs",
            id=seeded_id("job-era-0006"),
            kind="deadline_reminder",
            status="pending",
            due_at="2026-09-30T09:00:00.000000+00:00",
            next_attempt_at=None,
            dedup_key=f"deadline:{task_id}:1440",
            payload_json=f'{{"task_id":"{task_id}","offset_minutes":1440}}',
            attempts=0,
            last_error=None,
            created_at=STAMP,
            updated_at=STAMP,
            completed_at=None,
            cancelled_at=None,
            claim_token=None,
            claimed_by=None,
            claimed_at=None,
            lease_expires_at=None,
        )
        seeded["tasks"] = task_id
        seeded["scheduled_jobs"] = seeded_id("job-era-0006")
    elif prefix == "0009":
        message_id = seeded_id("message-era-0009")
        insert(
            connection,
            "mail_messages",
            id=message_id,
            account_id="smail",
            message_id_header="<era@example.edu>",
            in_reply_to_header=None,
            references_json="[]",
            subject="An old message",
            from_address="ada@example.edu",
            to_addresses_json='["student@example.edu"]',
            cc_addresses_json="[]",
            date_header=None,
            sent_at=None,
            body_text="An old body.",
            body_status="available",
            raw_sha256=None,
            content_fingerprint=DIGEST,
            raw_storage_key=None,
            size_bytes=13,
            parse_warnings=0,
            first_seen_at=STAMP,
            last_seen_at=STAMP,
        )
        insert(
            connection,
            "mail_drafts",
            id=seeded_id("draft-era-0009"),
            account_id="smail",
            thread_id=None,
            reply_to_message_id=message_id,
            to_addresses_json='["ada@example.edu"]',
            subject="Re: An old message",
            body_text="An old draft.",
            needs_user_input_json="[]",
            origin="model_generated",
            version=1,
            generation_input_fingerprint=DIGEST,
            prompt_version=1,
            created_at=STAMP,
            updated_at=STAMP,
        )
        seeded["mail_messages"] = message_id
        seeded["mail_drafts"] = seeded_id("draft-era-0009")
    elif prefix == "0012":
        case_id = seeded_id("case-era-0012")
        insert(
            connection,
            "cases",
            id=case_id,
            title="An old case",
            status="open",
            created_at=STAMP,
            updated_at=STAMP,
            completed_at=None,
            cancelled_at=None,
        )
        insert(
            connection,
            "action_requests",
            id=seeded_id("action-era-0012"),
            case_id=case_id,
            action_type="mail.send",
            payload_json='{"schema_version":1}',
            fingerprint=DIGEST,
            status="prepared",
            created_at=STAMP,
            executed_at=None,
            cancelled_at=None,
        )
        insert(
            connection,
            "mobile_pairing_tokens",
            id=seeded_id("pairing-era-0012"),
            token_hash=DIGEST,
            created_at=STAMP,
            expires_at="2026-09-26T09:10:00.000000+00:00",
            consumed_at=None,
        )
        seeded["cases"] = case_id
        seeded["action_requests"] = seeded_id("action-era-0012")
    elif prefix == "0015":
        correction_id = seeded_id("correction-era-0015")
        insert(
            connection,
            "corrections",
            id=correction_id,
            text="My office is Room 302.",
            created_at=STAMP,
        )
        insert(
            connection,
            "fact_candidates",
            id=seeded_id("candidate-era-0015"),
            fact_key="profile.office",
            value="Room 302",
            correction_id=correction_id,
            status="pending",
            created_at=STAMP,
            proposed_valid_until=None,
            resolved_at=None,
        )
        insert(
            connection,
            "manual_inputs",
            id=seeded_id("manual-era-0015"),
            source="manual",
            text="A forwarded notice.",
            content_sha256=DIGEST,
            created_at=STAMP,
        )
        seeded["corrections"] = correction_id
        seeded["fact_candidates"] = seeded_id("candidate-era-0015")
        seeded["manual_inputs"] = seeded_id("manual-era-0015")
    return seeded


def count_row(connection: sqlite3.Connection, table: str, identifier: str) -> int:
    """How many rows of `table` carry this id (the id column differs for `storage_roots`)."""
    column = "root_id" if table == "storage_roots" else "id"
    row = connection.execute(
        f"SELECT count(*) AS total FROM {table} WHERE {column} = ?", (identifier,)
    ).fetchone()
    return int(row["total"])


__all__ = [
    "ALL_VERSIONS",
    "DIGEST",
    "REPOSITORY_MIGRATIONS",
    "REPRESENTATIVE_PREFIXES",
    "STAMP",
    "count_row",
    "database_at_prefix",
    "insert",
    "prefix_directory",
    "seed_era",
    "seeded_id",
]
