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


# ------------------------------------------- conversational external reviews (ADR-0045 §24-§25)

_REVIEW_INSERT = (
    "INSERT INTO conversation_external_reviews (id, conversation_operation_id, thread_id, "
    "action_request_id, action_type, action_fingerprint, status, expires_at, execution_run_id, "
    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def _seed_review(database: Database, **overrides: object) -> dict[str, str]:
    """A real review row plus every row it points at, written the way the store writes them."""
    from assistant.domain.action import action_payload_fingerprint

    payload = {"schema_version": 1, "fields": [], "service_identity": "nju-ehall/证明书申请"}
    fingerprint = action_payload_fingerprint(payload)
    canonical = __import__("json").dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    identifiers = {
        "thread": "22222222-2222-4222-8222-222222222222",
        "message": "33333333-3333-4333-8333-333333333333",
        "turn": "44444444-4444-4444-8444-444444444444",
        "operation": "55555555-5555-4555-8555-555555555555",
        "case": "66666666-6666-4666-8666-666666666666",
        "action": "77777777-7777-4777-8777-777777777777",
        "approval": "88888888-8888-4888-8888-888888888888",
        "run": "99999999-9999-4999-8999-999999999999",
        "review": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    }
    action_status = str(overrides.pop("action_status", "prepared"))
    _write(
        database,
        (
            "INSERT INTO conversation_threads (id, title, status, created_at, updated_at, "
            "archived_at) VALUES (?, 'Sprint', 'active', ?, ?, NULL)",
            (identifiers["thread"], STAMP, STAMP),
        ),
        (
            "INSERT INTO conversation_messages (id, thread_id, role, text, created_at) "
            "VALUES (?, ?, 'user', 'apply', ?)",
            (identifiers["message"], identifiers["thread"], STAMP),
        ),
        (
            "INSERT INTO conversation_turns (id, thread_id, user_message_id, assistant_message_id,"
            " interpreter_version, context_fingerprint, status, created_at, completed_at) "
            "VALUES (?, ?, ?, NULL, 'v1', ?, 'planned', ?, NULL)",
            (identifiers["turn"], identifiers["thread"], identifiers["message"], "f" * 64, STAMP),
        ),
        (
            "INSERT INTO conversation_operations (id, turn_id, ordinal, operation_type, "
            "arguments_json, operation_fingerprint, status, result_kind, result_ref, "
            "confirmation_expires_at, created_at, updated_at) "
            "VALUES (?, ?, 0, 'ehall.certificate.prepare', '{}', ?, 'applied', 'ehall_prepared', "
            "'a', NULL, ?, ?)",
            (identifiers["operation"], identifiers["turn"], "e" * 64, STAMP, STAMP),
        ),
        (
            "INSERT INTO cases (id, title, status, created_at, updated_at, completed_at, "
            "cancelled_at) VALUES (?, 'Certificate', 'open', ?, ?, NULL, NULL)",
            (identifiers["case"], STAMP, STAMP),
        ),
        (
            "INSERT INTO action_requests (id, case_id, action_type, payload_json, fingerprint, "
            "status, created_at, executed_at, cancelled_at) "
            "VALUES (?, ?, 'ehall.submit-certificate', ?, ?, ?, ?, NULL, NULL)",
            (
                identifiers["action"],
                identifiers["case"],
                canonical,
                fingerprint,
                action_status,
                STAMP,
            ),
        ),
    )
    _write(
        database,
        (
            _REVIEW_INSERT,
            (
                identifiers["review"],
                identifiers["operation"],
                identifiers["thread"],
                identifiers["action"],
                str(overrides.get("action_type", "ehall.submit-certificate")),
                str(overrides.get("action_fingerprint", fingerprint)),
                str(overrides.get("status", "waiting")),
                str(overrides.get("expires_at", LATER)),
                overrides.get("execution_run_id"),
                STAMP,
                STAMP,
            ),
        ),
    )
    identifiers["fingerprint"] = fingerprint
    return identifiers


def _ignore_constraints(database: Database, *statements: tuple[str, tuple[object, ...]]) -> None:
    """Write a row the schema would refuse, the way a restore from another binary could."""
    connection = sqlite3.connect(str(database.path))
    try:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        for sql, parameters in statements:
            connection.execute(sql, parameters)
        connection.commit()
    finally:
        connection.close()


def test_a_healthy_external_review_reports_ok(tmp_path: Path) -> None:
    database = _runtime(tmp_path)
    _seed_review(database)

    section = _section(database, "external_reviews")

    assert section.severity is IntegritySeverity.OK
    assert "1 external review(s) checked" in section.summary


def test_a_review_that_names_an_unreviewed_capability_is_critical(tmp_path: Path) -> None:
    """The closed set is audited, not assumed: a restored row can carry anything."""
    database = _runtime(tmp_path)
    identifiers = _seed_review(database)
    _ignore_constraints(
        database,
        (
            "UPDATE conversation_external_reviews SET action_type = 'ehall.drop-course' "
            "WHERE id = ?",
            (identifiers["review"],),
        ),
    )

    section = _section(database, "external_reviews")

    assert section.severity is IntegritySeverity.CRITICAL
    assert any("unreviewed action type" in finding for finding in section.findings)


def test_a_review_whose_payload_changed_is_critical(tmp_path: Path) -> None:
    database = _runtime(tmp_path)
    identifiers = _seed_review(database)
    _write(
        database,
        (
            "UPDATE action_requests SET payload_json = ? WHERE id = ?",
            ('{"a":1}', identifiers["action"]),
        ),
    )

    section = _section(database, "external_reviews")

    assert section.severity is IntegritySeverity.CRITICAL
    assert any("no longer matches the payload" in finding for finding in section.findings)


def test_a_review_with_no_action_is_critical(tmp_path: Path) -> None:
    database = _runtime(tmp_path)
    identifiers = _seed_review(database)
    _write(
        database,
        ("DELETE FROM action_requests WHERE id = ?", (identifiers["action"],)),
    )

    section = _section(database, "external_reviews")

    assert section.severity is IntegritySeverity.CRITICAL
    assert any("does not exist" in finding for finding in section.findings)


def test_a_review_that_claims_a_decision_with_no_run_is_critical(tmp_path: Path) -> None:
    """A succeeded review with no execution run is tampering: the run is the evidence."""
    database = _runtime(tmp_path)
    identifiers = _seed_review(database)
    _ignore_constraints(
        database,
        (
            "UPDATE conversation_external_reviews SET status = 'succeeded' WHERE id = ?",
            (identifiers["review"],),
        ),
    )

    section = _section(database, "external_reviews")

    assert section.severity is IntegritySeverity.CRITICAL
    assert any("no execution to show" in finding for finding in section.findings)


def test_a_review_that_claims_approval_without_one_is_reported(tmp_path: Path) -> None:
    """An approved review with no approval row is a FAIL: inconsistent, not necessarily hostile."""
    database = _runtime(tmp_path)
    identifiers = _seed_review(database)
    _ignore_constraints(
        database,
        (
            "UPDATE conversation_external_reviews SET status = 'approved' WHERE id = ?",
            (identifiers["review"],),
        ),
    )

    section = _section(database, "external_reviews")

    assert section.severity is IntegritySeverity.FAIL
    assert any("no approval recorded" in finding for finding in section.findings)


def test_a_waiting_review_on_a_spent_action_is_reported(tmp_path: Path) -> None:
    """Waiting on an action that already executed is a FAIL: nothing here is tampering."""
    database = _runtime(tmp_path)
    identifiers = _seed_review(database)
    _write(
        database,
        (
            "UPDATE action_requests SET status = 'executed', executed_at = ? WHERE id = ?",
            (STAMP, identifiers["action"]),
        ),
    )

    section = _section(database, "external_reviews")

    assert section.severity is IntegritySeverity.FAIL
    assert any("waiting on an action that is executed" in finding for finding in section.findings)
