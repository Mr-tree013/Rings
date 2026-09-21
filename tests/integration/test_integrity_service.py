"""`pw integrity check` over a real runtime (ADR-0031).

The checker is read-only and offline, and these tests hold it to both: a healthy runtime passes, a
tampered action payload is `CRITICAL` rather than repaired, a missing or mismatched content object
is `FAIL`, a pending migration is `WARN` (reported, never applied), and an offline removable vault
is reported as offline instead of being treated as corruption.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from assistant.domain.integrity import IntegritySeverity
from assistant.store.db import Database
from tests.support.ops import RuntimeFixture


@pytest.fixture
def runtime(tmp_path: Path) -> RuntimeFixture:
    return RuntimeFixture(tmp_path / "data" / "growing-assistant")


async def _seed(runtime: RuntimeFixture) -> tuple[str, str]:
    await runtime.add_task()
    await runtime.add_case()
    mail_key = await runtime.add_raw_mail()
    web_key = await runtime.add_web_observation()
    await runtime.add_fact()
    return mail_key, web_key


async def test_a_healthy_runtime_passes(runtime: RuntimeFixture) -> None:
    await _seed(runtime)

    report = await runtime.integrity_service().check()

    assert report.passed is True
    assert report.worst is IntegritySeverity.OK
    assert report.section("database").severity is IntegritySeverity.OK
    assert report.section("content").summary == (
        "mail raw 1/1 valid, web snapshots 1/1 valid"
    )
    assert report.section("capabilities").severity is IntegritySeverity.OK


async def test_the_check_is_read_only(runtime: RuntimeFixture) -> None:
    await _seed(runtime)
    before = runtime.counts()

    report = await runtime.integrity_service().check()

    assert report.passed is True
    assert runtime.counts() == before


async def test_a_missing_content_object_is_a_failure(runtime: RuntimeFixture) -> None:
    mail_key, _ = await _seed(runtime)
    runtime.delete_object(mail_key)

    report = await runtime.integrity_service().check()

    section = report.section("content")
    assert section.severity is IntegritySeverity.FAIL
    assert report.passed is False
    assert any(mail_key in finding for finding in section.findings)


async def test_a_mismatched_content_object_is_a_failure(runtime: RuntimeFixture) -> None:
    _, web_key = await _seed(runtime)
    runtime.write_raw_bytes(web_key, b"a different page")

    report = await runtime.integrity_service().check()

    assert report.section("content").severity is IntegritySeverity.FAIL
    assert report.passed is False


async def test_a_tampered_action_payload_is_critical(runtime: RuntimeFixture) -> None:
    """§30: the fingerprint is re-derived, and a mismatch is never repaired."""
    await _seed(runtime)
    case = await runtime.cases.list_cases(limit=1)
    action = await runtime.cases.prepare_action(
        case[0].id, "mail.send", {"to": "ada@example.edu", "subject": "x", "body": "y"}
    )
    with runtime.database.connect() as connection:
        # Tamper past the store's own guard: the payload and the fingerprint now disagree.
        connection.execute(
            "UPDATE action_requests SET payload_json = ? WHERE id = ?",
            ('{"to":"attacker@example.com"}', str(action.id)),
        )

    report = await runtime.integrity_service().check()

    section = report.section("capabilities")
    assert section.severity is IntegritySeverity.CRITICAL
    assert report.has_critical is True
    assert report.passed is False
    assert any(str(action.id) in finding for finding in section.findings)
    # The finding names the action; it never prints the payload.
    assert "attacker@example.com" not in " ".join(section.findings)


async def test_a_broken_approval_link_is_critical(runtime: RuntimeFixture) -> None:
    await _seed(runtime)
    case = await runtime.cases.list_cases(limit=1)
    action = await runtime.cases.prepare_action(
        case[0].id, "mail.send", {"to": "ada@example.edu", "subject": "x", "body": "y"}
    )
    from assistant.application.approval_service import ApprovalService
    from tests.support.actions import SECRET_TOKEN, FixedTokenFactory

    approvals = ApprovalService(
        runtime.actions, runtime.clock, token_factory=FixedTokenFactory(SECRET_TOKEN)
    )
    issued = await approvals.create_challenge(action.id)
    await approvals.approve(action.id, issued.token)
    with runtime.database.connect() as connection:
        connection.execute(
            "UPDATE approvals SET action_fingerprint = ? WHERE action_id = ?",
            ("f" * 64, str(action.id)),
        )

    report = await runtime.integrity_service().check()

    assert report.section("capabilities").severity is IntegritySeverity.CRITICAL


async def test_a_fact_whose_snapshot_drifted_is_critical(runtime: RuntimeFixture) -> None:
    await runtime.add_fact()
    with runtime.database.connect() as connection:
        connection.execute("UPDATE confirmed_facts SET value = 'Room 999'")

    report = await runtime.integrity_service().check()

    assert report.section("learning").severity is IntegritySeverity.CRITICAL


async def test_two_current_facts_for_one_key_are_critical(runtime: RuntimeFixture) -> None:
    await runtime.add_fact(key="profile.office", value="Room 302")
    await runtime.add_fact(key="profile.office", value="Room 320")
    with runtime.database.connect() as connection:
        # Defeat the partial index the way real corruption would: supersede neither row.
        connection.execute("DROP INDEX confirmed_facts_current_idx")
        connection.execute("UPDATE confirmed_facts SET superseded_at = NULL")

    report = await runtime.integrity_service().check()

    section = report.section("learning")
    assert section.severity is IntegritySeverity.CRITICAL
    assert any("profile.office" in finding for finding in section.findings)


async def test_a_pending_migration_is_reported_not_applied(runtime: RuntimeFixture) -> None:
    """§27: the check is read-only, so a reviewed-but-unapplied migration is a warning."""
    await _seed(runtime)
    temporary = runtime.runtime.parent / "migrations"
    temporary.mkdir(exist_ok=True)
    for path in Path("migrations").glob("*.sql"):
        (temporary / path.name).write_text("-- placeholder", encoding="utf-8")
    # One migration the reviewed files contain and the database has not applied. Its version has to
    # be *ahead* of every shipped one, or it collides with a file this build really ships.
    latest = max(
        int(path.name[:4]) for path in Path("migrations").glob("*.sql")
    )
    (temporary / f"{latest + 1:04d}_future.sql").write_text("SELECT 1;", encoding="utf-8")
    service = runtime.integrity_service()
    from assistant.adapters.ops.content_objects import RuntimeContentObjects
    from assistant.application.integrity_service import IntegrityService
    from assistant.store.integrity import SqliteIntegrityRepository

    service = IntegrityService(
        SqliteIntegrityRepository(runtime.database),
        RuntimeContentObjects(runtime.runtime),
        migrations_directory=temporary,
    )

    report = await service.check()

    section = report.section("migrations")
    assert section.severity is IntegritySeverity.WARN
    assert "PENDING" in section.summary
    assert report.passed is True  # a pending migration is not corruption
    assert runtime.counts()["tasks"] == 1


async def test_an_offline_vault_is_reported_as_offline(runtime: RuntimeFixture) -> None:
    """§29: a removed USB stick is not a corrupt runtime."""
    await _seed(runtime)
    with runtime.database.connect() as connection:
        connection.execute(
            "INSERT INTO storage_roots (root_id, kind, label, last_known_path, first_seen_at, "
            "last_seen_at, last_scanned_at) VALUES ('archive-main', 'vault', 'Archive', ?, ?, ?, "
            "NULL)",
            (
                str(runtime.runtime.parent / "usb" / "archive"),
                runtime.clock.now().isoformat(timespec="microseconds"),
                runtime.clock.now().isoformat(timespec="microseconds"),
            ),
        )

    report = await runtime.integrity_service().check()

    section = report.section("knowledge")
    assert section.severity is IntegritySeverity.WARN
    assert "offline" in section.summary
    assert report.passed is True


async def test_the_report_carries_no_content(runtime: RuntimeFixture) -> None:
    """§34: findings name entities and counts, never a body, a page or a value."""
    await _seed(runtime)

    report = await runtime.integrity_service().check()
    blob = " ".join(
        finding for section in report.sections for finding in section.findings
    ) + " ".join(section.summary for section in report.sections)

    for sentinel in ("SENTINEL-MAIL-BODY", "SENTINEL-PAGE-TEXT", "SENTINEL-FACT-NOTE"):
        assert sentinel not in blob


async def test_an_empty_runtime_passes(runtime: RuntimeFixture) -> None:
    report = await runtime.integrity_service().check()

    assert report.passed is True
    assert report.section("content").summary == (
        "mail raw 0/0 valid, web snapshots 0/0 valid"
    )
    _ = Database  # the fixture owns the database handle


# ------------------------------------------------------------- weekly commitments (ADR-0036)


async def test_a_healthy_runtime_with_weekly_commitments_passes(
    runtime: RuntimeFixture,
) -> None:
    await _seed(runtime)
    await runtime.add_weekly_rule()
    await runtime.add_weekly_rule(
        title="软件工程课", weekday=3, start="14:00", end="16:00"
    )

    report = await runtime.integrity_service().check()

    assert report.passed is True
    assert report.section("recurring").severity is IntegritySeverity.OK
    assert report.section("recurring").summary == "2 weekly rule(s) checked, 2 active"


async def test_a_rule_that_no_longer_hashes_to_its_fingerprint_is_critical(
    runtime: RuntimeFixture,
) -> None:
    """Stored authority disagreeing with itself, exactly like a rewritten action payload."""
    rule = await runtime.add_weekly_rule()
    with runtime.database.connect() as connection:
        connection.execute(
            "UPDATE recurring_calendar_rules SET title = ? WHERE id = ?",
            ("另一门课", str(rule.id)),
        )

    report = await runtime.integrity_service().check()

    section = report.section("recurring")
    assert section.severity is IntegritySeverity.CRITICAL
    assert report.passed is False
    assert any(str(rule.id)[:8] in finding for finding in section.findings)


async def test_a_rule_that_breaks_its_own_invariants_is_reported(
    runtime: RuntimeFixture,
) -> None:
    """A row written around the API still has to mean something."""
    rule = await runtime.add_weekly_rule()
    with runtime.database.connect() as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE recurring_calendar_rules SET weekday = 9 WHERE id = ?", (str(rule.id),)
        )

    report = await runtime.integrity_service().check()

    section = report.section("recurring")
    assert section.severity is IntegritySeverity.FAIL
    assert report.passed is False
    assert any("cannot be interpreted" in finding for finding in section.findings)


async def test_two_active_rules_with_one_meaning_are_reported(
    runtime: RuntimeFixture,
) -> None:
    """The unique index is what prevents this; the check is what notices if it is gone."""
    rule = await runtime.add_weekly_rule()
    with runtime.database.connect() as connection:
        connection.execute("DROP INDEX recurring_calendar_rules_active_idx")
        connection.execute(
            "INSERT INTO recurring_calendar_rules (id, title, weekday, start_local_time, "
            "end_local_time, timezone, starts_on, ends_on, status, rule_fingerprint, created_at, "
            "updated_at, retired_at) SELECT ?, title, weekday, start_local_time, end_local_time, "
            "timezone, starts_on, ends_on, status, rule_fingerprint, created_at, updated_at, "
            "retired_at FROM recurring_calendar_rules WHERE id = ?",
            (str(uuid4()), str(rule.id)),
        )

    report = await runtime.integrity_service().check()

    section = report.section("recurring")
    assert section.severity is IntegritySeverity.CRITICAL
    assert any("share one meaning" in finding for finding in section.findings)
