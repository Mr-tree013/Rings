"""`pw integrity check` over the v1.3 durable state (ADR-0042 §72, ADR-0044 §72).

Each test corrupts one thing the audit claims to catch and asserts the section says so. The
database refuses most of these shapes on its own, so the corruptions are written raw: an audit whose
findings cannot be provoked is an audit nobody has tested, and the whole point of this check is to
notice state that arrived some other way.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from assistant.application.integrity_service import IntegrityService
from assistant.domain.integrity import IntegritySeverity
from assistant.store.db import Database
from assistant.store.integrity import SqliteIntegrityRepository
from assistant.store.migrations import apply_migrations
from tests.support.fakes import FakeClock

NOW = datetime(2026, 9, 21, 4, 0, tzinfo=UTC)
STAMP = "2026-09-21T04:00:00.000000+00:00"
LATER = "2026-09-21T05:00:00.000000+00:00"


class _NullObjects:
    """A content-object reader for a runtime that references no stored object."""

    def missing(self, keys: object) -> tuple[str, ...]:
        return ()

    def verify(self, key: str, expected: str) -> bool:
        return True


def _runtime(tmp_path: Path) -> Database:
    clock = FakeClock(start=NOW)
    database = Database.at(tmp_path / "data" / "assistant.db")
    apply_migrations(database, clock=clock)
    return database


def _section(database: Database, name: str):
    """One audit section, read exactly the way `pw integrity check` reads it."""
    service = IntegrityService(
        SqliteIntegrityRepository(Database.read_only(database.path)), _NullObjects()
    )
    report = asyncio.run(service.check())
    return next(item for item in report.sections if item.name == name)


def _write(database: Database, *statements: tuple[str, tuple[object, ...]]) -> None:
    connection = sqlite3.connect(str(database.path))
    try:
        for sql, parameters in statements:
            connection.execute(sql, parameters)
        connection.commit()
    finally:
        connection.close()


_ATTENTION_INSERT = (
    "INSERT INTO attention_items (id, kind, source_type, source_id, dedupe_key, "
    "source_fingerprint, generation, status, severity, title, summary, created_at, updated_at, "
    "acknowledged_at, dismissed_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
    "?, ?, ?)"
)


def _attention(
    identifier: str = "11111111-1111-4111-8111-111111111111",
    *,
    fingerprint: str = "a" * 64,
    status: str = "open",
    acknowledged_at: str | None = None,
) -> tuple[str, tuple[object, ...]]:
    return (
        _ATTENTION_INSERT,
        (
            identifier,
            "task_overdue",
            "task",
            "22222222-2222-4222-8222-222222222222",
            f"task-overdue:{identifier}",
            fingerprint,
            1,
            status,
            "high",
            "测试",
            None,
            STAMP,
            acknowledged_at or STAMP,
            acknowledged_at,
            None,
            None,
        ),
    )


_TASK_INSERT = (
    "INSERT INTO tasks (id, title, status, priority, created_at, updated_at, estimated_minutes, "
    "description, completed_at, cancelled_at) VALUES (?, '写报告', 'open', 'normal', ?, ?, 120, "
    "NULL, NULL, NULL)"
)

_BLOCK_INSERT = (
    "INSERT INTO plan_blocks (id, task_id, starts_at, ends_at, created_at, updated_at, "
    "cancelled_at, origin, proposal_id, superseded_at, superseded_by_proposal_id) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, 'manual', NULL, ?, ?)"
)


def test_a_clean_runtime_reports_both_sections_ok(tmp_path: Path) -> None:
    database = _runtime(tmp_path)

    assert _section(database, "attention").severity is IntegritySeverity.OK
    assert _section(database, "planning").severity is IntegritySeverity.OK


def test_the_database_refuses_an_attention_row_its_own_lifecycle_disowns(
    tmp_path: Path,
) -> None:
    """The constraint is the first line, and the audit is the second.

    A row whose status and timestamps disagree cannot be written at all, which is why the audit
    check for it can never be provoked through SQL — and why testing the refusal, rather than the
    finding, is the honest assertion here.
    """
    database = _runtime(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        _write(database, _attention(acknowledged_at=LATER))


def test_the_database_refuses_a_non_sha256_fingerprint(tmp_path: Path) -> None:
    database = _runtime(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        _write(database, _attention(fingerprint="NOT-A-DIGEST"))


def test_a_half_recorded_supersession_is_a_failure(tmp_path: Path) -> None:
    database = _runtime(tmp_path)
    _write(
        database,
        (_TASK_INSERT, ("33333333-3333-4333-8333-333333333333", STAMP, STAMP)),
        (
            _BLOCK_INSERT,
            (
                "44444444-4444-4444-8444-444444444444",
                "33333333-3333-4333-8333-333333333333",
                "2026-09-22T01:00:00.000000+00:00",
                "2026-09-22T02:00:00.000000+00:00",
                STAMP,
                LATER,
                LATER,
                LATER,
                None,
            ),
        ),
    )

    section = _section(database, "planning")

    assert section.severity is IntegritySeverity.FAIL
    assert any("half of a supersession" in finding for finding in section.findings)


def test_the_database_refuses_inconsistent_planning_preferences(tmp_path: Path) -> None:
    database = _runtime(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        _write(
        database,
        (
            "INSERT INTO planning_preferences (id, day_start_local, day_end_local, "
            "max_daily_minutes, preferred_block_minutes, max_block_minutes, created_at, "
            "updated_at) VALUES ('current', 0, 1440, 360, 180, 120, ?, ?)",
            (STAMP, STAMP),
        ),
    )


def test_the_new_sections_are_always_present(tmp_path: Path) -> None:
    """A section that only appears sometimes is one nobody notices is missing."""
    database = _runtime(tmp_path)

    assert _section(database, "attention").summary
    assert _section(database, "planning").summary
