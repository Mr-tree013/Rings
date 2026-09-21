"""Helpers for the operational tests: a real runtime with real durable state (ADR-0031).

Everything here goes through the project's own stores and services — the raw mail store writes real
content-addressed objects, the snapshot store writes real normalized text, tasks and facts go
through their application services — so a backup in a test is a backup of a runtime that looks like
a used one rather than a fixture of empty tables.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from assistant.adapters.backup.archive import ZipBackupArchive
from assistant.adapters.mail.raw_store import RawMailStore
from assistant.adapters.ops.content_objects import RuntimeContentObjects
from assistant.adapters.runtime.permissions import ensure_private_directory
from assistant.adapters.web_watch.snapshot_store import WebSnapshotStore
from assistant.application.backup_service import MIGRATION_DIRECTORY, BackupService
from assistant.application.case_service import CaseService
from assistant.application.integrity_service import IntegrityService
from assistant.application.learning_service import LearningService
from assistant.application.task_service import CreateTask, TaskService
from assistant.domain.case import Case
from assistant.domain.task import Task, TaskPriority
from assistant.store.actions import SqliteActionRepository
from assistant.store.backup import SqliteRuntimeBackup
from assistant.store.cases import SqliteCaseRepository
from assistant.store.commitment import SqliteCommitmentRepository
from assistant.store.db import Database
from assistant.store.integrity import SqliteIntegrityRepository
from assistant.store.learning import SqliteLearningRepository
from assistant.store.mail import SqliteMailRepository
from assistant.store.migrations import apply_migrations
from tests.support.fakes import FakeClock

NOW = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)
APPLICATION_VERSION = "0.8.0"

MAIL_BODY = "Dear Ada, the report is attached. SENTINEL-MAIL-BODY"
PAGE_TEXT = "Notices\nRegistration closes Oct 25. SENTINEL-PAGE-TEXT\n"


@dataclass
class RuntimeFixture:
    """One temporary runtime directory with real durable state in it."""

    runtime: Path
    clock: FakeClock = field(default_factory=lambda: FakeClock(start=NOW))
    database: Database = field(init=False)
    mail: SqliteMailRepository = field(init=False)
    commitments: SqliteCommitmentRepository = field(init=False)
    actions: SqliteActionRepository = field(init=False)
    cases_repository: SqliteCaseRepository = field(init=False)

    def __post_init__(self) -> None:
        ensure_private_directory(self.runtime)
        self.database = Database.at(self.runtime / "assistant.db")
        apply_migrations(self.database, clock=self.clock)
        self.mail = SqliteMailRepository(self.database)
        self.commitments = SqliteCommitmentRepository(self.database)
        self.actions = SqliteActionRepository(self.database)
        self.cases_repository = SqliteCaseRepository(self.database)

    # ------------------------------------------------------------------ seeding

    @property
    def tasks(self) -> TaskService:
        """The task service over this runtime, with the default reminder offsets."""
        return TaskService(self.commitments, self.clock)

    @property
    def cases(self) -> CaseService:
        """The case service over this runtime."""
        return CaseService(self.cases_repository, self.actions, self.clock)

    @property
    def learning(self) -> LearningService:
        """The learning service over this runtime."""
        return LearningService(SqliteLearningRepository(self.database), self.clock)

    async def add_task(
        self, title: str = "Write the SE lab report", *, due_in_days: int | None = 2
    ) -> Task:
        """One open task, optionally with a deadline."""
        return await self.tasks.create_task(
            CreateTask(
                title=title,
                priority=TaskPriority.HIGH,
                estimated_minutes=120,
                due_at=None if due_in_days is None else NOW + timedelta(days=due_in_days),
            )
        )

    async def add_case(self, title: str = "Register for the course") -> Case:
        """One open case."""
        return await self.cases.create_case(title)

    async def add_raw_mail(self, *, account_id: str = "smail") -> str:
        """Store a raw RFC822 object and the message row that references it."""
        raw = (
            f"From: Ada <ada@example.edu>\r\nTo: student@example.edu\r\n"
            f"Subject: SE lab deadline\r\n"
            f"Message-ID: <{uuid4()}@example.edu>\r\n\r\n{MAIL_BODY}\r\n"
        ).encode()
        store = RawMailStore(self.runtime / "mail")
        stored = await store.store(raw)
        stamp = NOW.isoformat(timespec="microseconds")
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO mail_messages (id, account_id, message_id_header, "
                "references_json, to_addresses_json, cc_addresses_json, reply_to_addresses_json, "
                "body_status, raw_sha256, content_fingerprint, raw_storage_key, size_bytes, "
                "parse_warnings, first_seen_at, last_seen_at) VALUES (?, ?, '<m@example.edu>', "
                "'[]', '[]', '[]', '[]', 'available', ?, ?, ?, ?, 0, ?, ?)",
                (
                    str(uuid4()),
                    account_id,
                    stored.sha256,
                    stored.sha256,
                    stored.storage_key,
                    stored.size_bytes,
                    stamp,
                    stamp,
                ),
            )
        return stored.storage_key

    async def add_web_observation(
        self, *, target_id: str = "course-notices", text: str = PAGE_TEXT
    ) -> str:
        """Store a normalized page snapshot and the baseline observation that references it."""
        store = WebSnapshotStore(self.runtime)
        digest, key = await store.store_text(text)
        stamp = NOW.isoformat(timespec="microseconds")
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO web_observations (id, target_id, url, content_sha256, storage_key, "
                "previous_observation_id, is_baseline, fetched_at) VALUES (?, ?, "
                "'https://example.edu/notices', ?, ?, NULL, 1, ?)",
                (str(uuid4()), target_id, digest, key, stamp),
            )
        return key

    async def add_fact(self, *, key: str = "profile.office", value: str = "Room 302") -> None:
        """One confirmed fact, with the correction that justifies it."""
        proposal = await self.learning.propose_fact(key, value, "SENTINEL-FACT-NOTE")
        await self.learning.confirm_fact(proposal.candidate.id)

    async def add_weekly_rule(
        self,
        *,
        title: str = "计算机系统基础课",
        weekday: int = 1,
        start: str = "10:00",
        end: str = "12:00",
        timezone: str = "Asia/Shanghai",
    ):
        """One durable weekly commitment, created through the real application service."""
        from assistant.application.recurring_calendar_service import RecurringCalendarService
        from assistant.store.recurring_calendar import SqliteRecurringCalendarRepository

        return await RecurringCalendarService(
            SqliteRecurringCalendarRepository(self.database),
            self.clock,
            default_timezone=timezone,
        ).create_weekly(title=title, weekday=weekday, start=start, end=end)

    async def add_contact(
        self,
        *,
        display_name: str = "张老师",
        email_address: str = "zhang@example.edu",
    ):
        """One durable contact, created through the real application service."""
        from assistant.application.contacts import ContactService
        from assistant.store.contacts import SqliteContactRepository

        contact, _ = await ContactService(
            SqliteContactRepository(self.database), self.clock
        ).create(display_name=display_name, email_address=email_address)
        return contact

    # ------------------------------------------------------------------ services

    def backup_service(self) -> BackupService:
        """The backup service over this runtime."""
        return BackupService(
            self.runtime,
            SqliteRuntimeBackup(self.runtime / "assistant.db"),
            self.clock,
            ZipBackupArchive(),
            application_version=APPLICATION_VERSION,
            migrations_directory=MIGRATION_DIRECTORY,
        )

    def integrity_service(self) -> IntegrityService:
        """The integrity service over this runtime."""
        return IntegrityService(
            SqliteIntegrityRepository(self.database),
            RuntimeContentObjects(self.runtime),
            migrations_directory=MIGRATION_DIRECTORY,
        )

    # -------------------------------------------------------------------- helpers

    def counts(self) -> dict[str, int]:
        """Row counts for the tables a backup is expected to carry."""
        tables = (
            "tasks",
            "cases",
            "action_requests",
            "approvals",
            "execution_runs",
            "mail_messages",
            "web_observations",
            "corrections",
            "confirmed_facts",
            "mobile_sessions",
            "recurring_calendar_rules",
            "contacts",
            "new_mail_drafts",
            "mail_send_links",
        )
        with self.database.connect() as connection:
            return {
                table: int(
                    connection.execute(f"SELECT count(*) AS total FROM {table}").fetchone()[
                        "total"
                    ]
                )
                for table in tables
            }

    def raw_bytes(self, storage_key: str) -> bytes:
        """The bytes of one stored object, for tampering tests."""
        if storage_key.startswith("mail/raw/"):
            return (self.runtime / "mail" / storage_key).read_bytes()
        return (self.runtime / storage_key).read_bytes()

    def write_raw_bytes(self, storage_key: str, payload: bytes) -> None:
        """Overwrite one stored object, for tampering tests."""
        if storage_key.startswith("mail/raw/"):
            (self.runtime / "mail" / storage_key).write_bytes(payload)
            return
        (self.runtime / storage_key).write_bytes(payload)

    def delete_object(self, storage_key: str) -> None:
        """Remove one stored object, for missing-object tests."""
        if storage_key.startswith("mail/raw/"):
            (self.runtime / "mail" / storage_key).unlink()
            return
        (self.runtime / storage_key).unlink()

    def insert_mobile_rows(self) -> tuple[str, str]:
        """One unconsumed pairing code and one live session, as a restore must invalidate."""
        stamp = NOW.isoformat(timespec="microseconds")
        expiry = (NOW + timedelta(days=30)).isoformat(timespec="microseconds")
        pairing_id, session_id = str(uuid4()), str(uuid4())
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO mobile_pairing_tokens (id, token_hash, created_at, expires_at, "
                "consumed_at) VALUES (?, ?, ?, ?, NULL)",
                (pairing_id, "a" * 64, stamp, expiry),
            )
            connection.execute(
                "INSERT INTO mobile_sessions (id, session_hash, csrf_hash, created_at, "
                "expires_at, last_seen_at, revoked_at) VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (session_id, "b" * 64, "c" * 64, stamp, expiry, stamp),
            )
        return pairing_id, session_id

    def json_row(self, table: str, column: str, identifier: str) -> object:
        """Read one JSON column, for assertions that never print content."""
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT {column} FROM {table} WHERE id = ?", (identifier,)
            ).fetchone()
        return json.loads(str(row[column]))


__all__ = [
    "APPLICATION_VERSION",
    "MAIL_BODY",
    "NOW",
    "PAGE_TEXT",
    "RuntimeFixture",
]
