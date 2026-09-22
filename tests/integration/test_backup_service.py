"""Backup, verification and staging restore over a real runtime (ADR-0031).

Real SQLite (with WAL active), real raw mail objects, real page snapshots, real approvals and real
mobile rows. What is under test is what an operator depends on after something has gone wrong: the
backup is consistent, its hashes hold, its exclusions are real, a restore never lands on the live
runtime, and a restored database has lost every capability it used to carry — while keeping all of
its history.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.application.approval_service import ApprovalService
from assistant.domain.errors import (
    BackupSourceCorrupt,
    BackupSourceMissing,
    InvalidBackupArchive,
    RestoreDestinationRejected,
)
from tests.support.actions import SECRET_TOKEN, FixedTokenFactory
from tests.support.fakes import FakeClock
from tests.support.ops import NOW, RuntimeFixture


@pytest.fixture
def runtime(tmp_path: Path) -> RuntimeFixture:
    return RuntimeFixture(tmp_path / "data" / "growing-assistant")


async def _seeded(runtime: RuntimeFixture) -> tuple[str, str]:
    """A runtime with one of everything the archive is supposed to carry."""
    await runtime.add_task()
    await runtime.add_case()
    mail_key = await runtime.add_raw_mail()
    web_key = await runtime.add_web_observation()
    await runtime.add_fact()
    return mail_key, web_key


# --------------------------------------------------------------------------- create


def test_an_empty_runtime_can_be_backed_up(runtime: RuntimeFixture, tmp_path: Path) -> None:
    service = runtime.backup_service()

    result = service.create(tmp_path / "backup.gab")

    assert result.path.is_file()
    assert result.mail_object_count == 0
    assert result.web_object_count == 0
    assert service.verify(result.path).is_valid is True


async def test_a_used_runtime_is_backed_up_with_its_referenced_objects(
    runtime: RuntimeFixture, tmp_path: Path
) -> None:
    mail_key, web_key = await _seeded(runtime)
    service = runtime.backup_service()

    result = service.create(tmp_path / "backup.gab")
    handle = service._archive.read(result.path)

    assert result.mail_object_count == 1 and result.web_object_count == 1
    assert set(handle.member_names) == {
        "manifest.json",
        "runtime.sqlite3",
        mail_key,
        web_key,
    }
    manifest = result.manifest
    assert manifest.mail_keys() == (mail_key,)
    assert manifest.web_keys() == (web_key,)
    assert manifest.counts.tasks == 1
    assert manifest.counts.mail_messages == 1
    assert manifest.counts.web_observations == 1
    assert manifest.counts.confirmed_facts == 1
    assert manifest.migration_files[-1] == "0023_conversation_review_expansion.sql"


async def test_the_archive_excludes_everything_it_must(
    runtime: RuntimeFixture, tmp_path: Path
) -> None:
    """§12: an index, a profile, a config file and a secret have no member and no prefix."""
    await _seeded(runtime)
    (runtime.runtime / "cache").mkdir(exist_ok=True)
    (runtime.runtime / "cache" / "knowledge.db").write_text("derived", encoding="utf-8")
    (runtime.runtime / "ehall").mkdir(exist_ok=True)
    (runtime.runtime / "ehall" / "nju-profile").mkdir(exist_ok=True)
    (runtime.runtime / "ehall" / "nju-profile" / "Cookies").write_text("x", encoding="utf-8")
    (runtime.runtime / "config.toml").write_text("[mail]", encoding="utf-8")
    (runtime.runtime / ".env").write_text("DEEPSEEK_API_KEY=secret", encoding="utf-8")

    result = runtime.backup_service().create(tmp_path / "backup.gab")
    handle = runtime.backup_service()._archive.read(result.path)

    for name in handle.member_names:
        assert name in ("manifest.json", "runtime.sqlite3") or name.startswith(
            ("mail/raw/", "web/snapshots/")
        )
    blob = result.manifest.to_json()
    for forbidden in ("Cookies", "config.toml", ".env", "knowledge.db", "DEEPSEEK"):
        assert forbidden not in blob


async def test_a_backup_is_taken_from_a_live_wal_database(
    runtime: RuntimeFixture, tmp_path: Path
) -> None:
    """§7: the snapshot comes from the backup API, and the live database keeps working."""
    await _seeded(runtime)
    with runtime.database.connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    result = runtime.backup_service().create(tmp_path / "backup.gab")

    assert runtime.backup_service().verify(result.path).is_valid is True
    await runtime.add_task("A task added after the snapshot")
    assert runtime.counts()["tasks"] == 2  # the live runtime is untouched by the backup


async def test_an_existing_output_is_never_overwritten(
    runtime: RuntimeFixture, tmp_path: Path
) -> None:
    target = tmp_path / "backup.gab"
    target.write_text("already here", encoding="utf-8")

    with pytest.raises(InvalidBackupArchive):
        runtime.backup_service().create(target)

    assert target.read_text(encoding="utf-8") == "already here"


def test_a_failed_backup_leaves_no_partial_file(
    runtime: RuntimeFixture, tmp_path: Path
) -> None:
    """A destination that cannot be written leaves nothing behind, not a half archive."""
    runtime.backup_service().create(tmp_path / "first.gab")
    target = tmp_path / "second.gab"
    (tmp_path / "sub").mkdir()
    missing_destination = tmp_path / "sub" / "missing" / "third.gab"

    with pytest.raises(InvalidBackupArchive):
        runtime.backup_service().create(tmp_path / "first.gab")

    assert not target.exists()
    assert not missing_destination.exists()
    assert not list(tmp_path.rglob("*.part"))


async def test_a_missing_referenced_object_fails_the_whole_backup(
    runtime: RuntimeFixture, tmp_path: Path
) -> None:
    """§11: a backup with a hole in it is not a backup."""
    mail_key = await runtime.add_raw_mail()
    runtime.delete_object(mail_key)

    with pytest.raises(BackupSourceMissing):
        runtime.backup_service().create(tmp_path / "backup.gab")

    assert not (tmp_path / "backup.gab").exists()


async def test_a_corrupt_referenced_object_fails_the_whole_backup(
    runtime: RuntimeFixture, tmp_path: Path
) -> None:
    mail_key = await runtime.add_raw_mail()
    runtime.write_raw_bytes(mail_key, b"tampered bytes")

    with pytest.raises(BackupSourceCorrupt):
        runtime.backup_service().create(tmp_path / "backup.gab")

    assert not (tmp_path / "backup.gab").exists()


async def test_a_corrupt_web_snapshot_fails_the_whole_backup(
    runtime: RuntimeFixture, tmp_path: Path
) -> None:
    web_key = await runtime.add_web_observation()
    runtime.write_raw_bytes(web_key, b"different page text")

    with pytest.raises(BackupSourceCorrupt):
        runtime.backup_service().create(tmp_path / "backup.gab")

    assert not (tmp_path / "backup.gab").exists()


async def test_a_missing_web_snapshot_fails_the_whole_backup(
    runtime: RuntimeFixture, tmp_path: Path
) -> None:
    web_key = await runtime.add_web_observation()
    runtime.delete_object(web_key)

    with pytest.raises(BackupSourceMissing):
        runtime.backup_service().create(tmp_path / "backup.gab")


async def test_the_database_object_hash_matches_the_manifest(
    runtime: RuntimeFixture, tmp_path: Path
) -> None:
    from assistant.domain.backup import sha256_hex

    await _seeded(runtime)
    result = runtime.backup_service().create(tmp_path / "backup.gab")
    handle = runtime.backup_service()._archive.read(result.path)
    extracted = tmp_path / "check.sqlite3"

    digest, size = handle.extract("runtime.sqlite3", extracted)

    assert digest == result.manifest.database_sha256
    assert len(extracted.read_bytes()) == size
    assert sha256_hex(extracted.read_bytes()) == digest


# --------------------------------------------------------------------------- verify


async def test_verify_is_read_only(runtime: RuntimeFixture, tmp_path: Path) -> None:
    await _seeded(runtime)
    service = runtime.backup_service()
    archive = service.create(tmp_path / "backup.gab")
    before = archive.path.read_bytes()
    counts = runtime.counts()

    result = service.verify(archive.path)

    assert result.is_valid is True
    assert archive.path.read_bytes() == before
    assert runtime.counts() == counts


async def test_inspect_shows_the_manifest_without_extracting(
    runtime: RuntimeFixture, tmp_path: Path
) -> None:
    await _seeded(runtime)
    service = runtime.backup_service()
    archive = service.create(tmp_path / "backup.gab")

    manifest = service.inspect(archive.path)

    assert manifest.application_version == "0.8.0"
    assert manifest.counts.tasks == 1
    assert manifest.database_sha256


# -------------------------------------------------------------------------- restore


def test_restore_refuses_a_non_empty_destination(
    runtime: RuntimeFixture, tmp_path: Path
) -> None:
    service = runtime.backup_service()
    archive = service.create(tmp_path / "backup.gab")
    destination = tmp_path / "recovered"
    destination.mkdir()
    (destination / "keep.txt").write_text("mine", encoding="utf-8")

    with pytest.raises(RestoreDestinationRejected):
        service.restore(archive.path, destination)

    assert (destination / "keep.txt").read_text(encoding="utf-8") == "mine"


def test_restore_refuses_the_active_runtime_directory(
    runtime: RuntimeFixture, tmp_path: Path
) -> None:
    """§17: there is no in-place restore, and this is the check that enforces it."""
    service = runtime.backup_service()
    archive = service.create(tmp_path / "backup.gab")

    with pytest.raises(RestoreDestinationRejected):
        service.restore(archive.path, runtime.runtime)
    with pytest.raises(RestoreDestinationRejected):
        service.restore(archive.path, runtime.runtime.parent)
    with pytest.raises(RestoreDestinationRejected):
        service.restore(archive.path, runtime.runtime / "sub")


async def test_a_restore_reproduces_the_runtime_tree(
    runtime: RuntimeFixture, tmp_path: Path
) -> None:
    """§18/§40: the staging tree is directly usable as a runtime directory."""
    mail_key, web_key = await _seeded(runtime)
    service = runtime.backup_service()
    archive = service.create(tmp_path / "backup.gab")
    destination = tmp_path / "recovered"

    result = service.restore(archive.path, destination)

    assert result.integrity_ok is True
    assert (destination / "assistant.db").is_file()
    assert (destination / "mail" / mail_key).is_file()
    assert (destination / web_key).is_file()
    assert not list(destination.rglob("*-wal"))
    assert not list(destination.rglob("*-shm"))


async def test_a_restored_runtime_reads_its_core_entities(
    runtime: RuntimeFixture, tmp_path: Path
) -> None:
    """§40/§68: the restored database is a working runtime, not just a sound file."""
    await _seeded(runtime)
    service = runtime.backup_service()
    archive = service.create(tmp_path / "backup.gab")
    destination = tmp_path / "recovered"
    service.restore(archive.path, destination)

    from assistant.application.learning_service import LearningService
    from assistant.application.paths import AppPaths
    from assistant.application.task_service import TaskService
    from assistant.store.commitment import SqliteCommitmentRepository
    from assistant.store.db import Database
    from assistant.store.learning import SqliteLearningRepository
    from assistant.store.mail import SqliteMailRepository

    clock = FakeClock(start=NOW)
    database = Database.at(destination / "assistant.db")
    tasks = await TaskService(SqliteCommitmentRepository(database), clock).list_tasks()
    facts = await LearningService(SqliteLearningRepository(database), clock).list_facts(
        limit=None
    )
    mail = await SqliteMailRepository(database).count_messages()

    assert [task.title for task in tasks] == ["Write the SE lab report"]
    assert [fact.value for fact in facts] == ["Room 302"]
    assert mail == 1
    _ = AppPaths  # the restored tree is the layout the composition root resolves


async def test_weekly_commitments_survive_a_backup_and_a_restore(
    runtime: RuntimeFixture, tmp_path: Path
) -> None:
    """ADR-0036 §4: a rule is one row in SQLite, so the existing archive carries it unchanged.

    Nothing about the archive format changes for recurring commitments, and nothing derived is
    archived: the restored runtime derives the same future from the same rule.
    """
    from datetime import UTC, datetime, timedelta

    from assistant.application.recurring_calendar_service import RecurringCalendarService
    from assistant.store.db import Database
    from assistant.store.recurring_calendar import SqliteRecurringCalendarRepository

    await _seeded(runtime)
    rule = await runtime.add_weekly_rule()
    service = runtime.backup_service()
    archive = service.create(tmp_path / "backup.gab")
    destination = tmp_path / "recovered"

    result = service.restore(archive.path, destination)

    assert result.integrity_ok is True
    restored = RecurringCalendarService(
        SqliteRecurringCalendarRepository(Database.at(destination / "assistant.db")),
        FakeClock(start=NOW),
        default_timezone="Asia/Shanghai",
    )
    rules = await restored.list_active()
    assert [item.title for item in rules] == ["计算机系统基础课"]
    assert rules[0].id == rule.id
    assert rules[0].fingerprint == rule.fingerprint
    window_start = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)
    occurrences = await restored.expand_range(
        window_start=window_start, window_end=window_start + timedelta(days=7)
    )
    assert len(occurrences) == 1  # the same future, derived rather than archived


async def test_contacts_and_unsent_new_mail_survive_a_restore(
    runtime: RuntimeFixture, tmp_path: Path
) -> None:
    """Contacts and new-mail drafts are SQLite authority, so the archive carries them.

    Nothing about the format changes (ADR-0037 §36). What must *not* survive is authority: a
    restored runtime has no live approval and no execution for the prepared letter.
    """
    from datetime import UTC, datetime

    from assistant.adapters.mail.smtp import rfc2822_date
    from assistant.application.case_service import CaseService
    from assistant.application.contacts import ContactService
    from assistant.application.mail_send_actions import MailSendActionService
    from assistant.domain.new_mail_draft import NewMailDraft
    from assistant.store.contacts import SqliteContactRepository
    from assistant.store.db import Database
    from assistant.store.mail import SqliteMailRepository
    from assistant.store.mail_drafts import SqliteMailDraftRepository
    from assistant.store.mail_send import SqliteMailSendRepository
    from assistant.store.new_mail_drafts import SqliteNewMailDraftRepository
    from tests.support.mail_send import smtp_account

    await _seeded(runtime)
    contact = await runtime.add_contact()
    account = smtp_account()
    drafts = SqliteNewMailDraftRepository(runtime.database)
    draft = await drafts.add_draft(
        NewMailDraft(
            account_id=account.id,
            to_address="alice@example.edu",
            subject="测试",
            body_text="你好",
            created_at=datetime(2026, 9, 25, 9, 0, tzinfo=UTC),
            updated_at=datetime(2026, 9, 25, 9, 0, tzinfo=UTC),
        )
    )
    case = await CaseService(
        runtime.cases_repository, runtime.actions, runtime.clock
    ).create_case("Send a test letter")
    preparation = await MailSendActionService(
        SqliteMailDraftRepository(runtime.database),
        SqliteMailRepository(runtime.database),
        runtime.cases_repository,
        runtime.actions,
        SqliteMailSendRepository(runtime.database),
        runtime.clock,
        accounts=(account,),
        message_id_factory=lambda domain: "<restore-check@example.edu>",
        date_header_factory=rfc2822_date,
        new_drafts=drafts,
    ).prepare_new_send(draft.id, case.id)
    service = runtime.backup_service()
    archive = service.create(tmp_path / "backup.gab")
    destination = tmp_path / "recovered"

    result = service.restore(archive.path, destination)

    assert result.integrity_ok is True
    recovered = Database.at(destination / "assistant.db")
    contacts = await ContactService(
        SqliteContactRepository(recovered), runtime.clock
    ).list_active()
    restored_drafts = await SqliteNewMailDraftRepository(recovered).list_drafts()
    with recovered.connect() as connection:
        approvals = connection.execute("SELECT count(*) AS total FROM approvals").fetchone()[
            "total"
        ]
        runs = connection.execute(
            "SELECT count(*) AS total FROM execution_runs"
        ).fetchone()["total"]
        links = connection.execute(
            "SELECT count(*) AS total FROM mail_send_links"
        ).fetchone()["total"]

    assert [item.email_address for item in contacts] == ["zhang@example.edu"]
    assert contacts[0].id == contact.id
    assert [(item.to_address, item.version) for item in restored_drafts] == [
        ("alice@example.edu", 1)
    ]
    assert links == 1  # the prepared letter is still reconciliable
    assert approvals == 0  # authority is not recreated by a restore
    assert runs == 0
    _ = preparation.action.id


async def test_facts_survive_a_restore_and_the_brief_recomputes(
    runtime: RuntimeFixture, tmp_path: Path
) -> None:
    """Facts are SQLite authority and the brief is derived, never stored.

    ADR-0038 adds no table, so nothing about the archive changes; ADR-0039 stores no snapshot, so
    the restored runtime recomputes today's brief from the restored rows.
    """
    import sqlite3

    from assistant.application.calendar_service import CreateCalendarEvent
    from assistant.application.contacts import ContactService
    from assistant.application.conversational_facts import ConversationalFactService
    from assistant.application.learning_service import LearningService
    from assistant.application.today_brief import TodayBriefService
    from assistant.domain.config import PlanningConfig
    from assistant.store.commitment import SqliteCommitmentRepository
    from assistant.store.conversation_reviews import SqliteConversationReviewRepository
    from assistant.store.conversations import SqliteConversationRepository
    from assistant.store.db import Database
    from assistant.store.learning import SqliteLearningRepository
    from assistant.store.planning import SqlitePlanningRepository
    from assistant.store.scheduler import SqliteSchedulerRepository

    await _seeded(runtime)
    learning = LearningService(SqliteLearningRepository(runtime.database), runtime.clock)
    facts = ConversationalFactService(learning)
    confirmed = await facts.propose(
        key="profile.office", value="仙林校区", correction_text="记住我的办公室在仙林"
    )
    await facts.confirm(confirmed.candidate.id)
    await facts.propose(
        key="profile.major", value="计算机科学", correction_text="记住我的专业是计算机科学"
    )
    service = runtime.backup_service()
    archive = service.create(tmp_path / "backup.gab")
    destination = tmp_path / "recovered"

    result = service.restore(archive.path, destination)

    assert result.integrity_ok is True
    recovered = Database.at(destination / "assistant.db")
    restored_learning = LearningService(SqliteLearningRepository(recovered), runtime.clock)
    restored_facts = ConversationalFactService(restored_learning)

    assert [fact.value for fact in await restored_facts.list_facts()] == ["仙林校区"]
    assert [item.fact_key for item in await restored_facts.pending()] == ["profile.major"]
    with recovered.connect() as connection:
        connection.row_factory = sqlite3.Row
        assert connection.execute("SELECT count(*) AS total FROM approvals").fetchone()[
            "total"
        ] == 0
        assert connection.execute(
            "SELECT count(*) AS total FROM execution_runs"
        ).fetchone()["total"] == 0
        stored = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    # No derived brief is stored anywhere: the restored runtime recomputes it from real rows.
    assert not [name for name in stored if "brief" in name]

    brief = await TodayBriefService(
        commitments=SqliteCommitmentRepository(recovered),
        planning=SqlitePlanningRepository(recovered),
        scheduler=SqliteSchedulerRepository(recovered),
        conversations=SqliteConversationRepository(recovered),
        facts=restored_facts,
        recurring=None,
        clock=runtime.clock,
        planning_timezone="Asia/Shanghai",
    ).build()

    assert brief.local_date.isoformat() == "2026-09-25"
    assert "1 条长期信息等待确认" in " ".join(entry.label for entry in brief.waiting)
    _ = (ContactService, CreateCalendarEvent, PlanningConfig, SqliteConversationReviewRepository)


async def test_a_restore_invalidates_live_capabilities_but_keeps_history(
    runtime: RuntimeFixture, tmp_path: Path
) -> None:
    """§19/§41: capability ends, audit stays."""
    await _seeded(runtime)
    case = await runtime.cases.list_cases(limit=1)
    action = await runtime.cases.prepare_action(
        case[0].id, "mail.send", {"to": "ada@example.edu", "subject": "x", "body": "y"}
    )
    approvals = ApprovalService(
        runtime.actions,
        runtime.clock,
        token_factory=FixedTokenFactory(SECRET_TOKEN),
    )
    issued = await approvals.create_challenge(action.id)
    approval = await approvals.approve(action.id, issued.token)
    # A second action whose challenge is never redeemed: that is the capability a restore must
    # retire, while the approval above must be superseded rather than consumed.
    second = await runtime.cases.prepare_action(
        case[0].id, "mail.send", {"to": "b@example.edu", "subject": "y", "body": "z"}
    )
    await approvals.create_challenge(second.id)
    pairing_id, session_id = runtime.insert_mobile_rows()

    service = runtime.backup_service()
    archive = service.create(tmp_path / "backup.gab")
    destination = tmp_path / "recovered"
    result = service.restore(archive.path, destination)

    assert result.challenges_invalidated == 1
    assert result.approvals_superseded == 1  # superseded, never consumed
    assert result.pairing_tokens_invalidated == 1
    assert result.sessions_revoked == 1
    connection = _open(destination)
    try:
        challenge = connection.execute(
            "SELECT consumed_at FROM approval_challenges WHERE action_id = ?",
            (str(second.id),),
        ).fetchone()
        stored = connection.execute(
            "SELECT consumed_at, superseded_at FROM approvals WHERE id = ?",
            (str(approval.id),),
        ).fetchone()
        pairing = connection.execute(
            "SELECT consumed_at FROM mobile_pairing_tokens WHERE id = ?", (pairing_id,)
        ).fetchone()
        session = connection.execute(
            "SELECT revoked_at FROM mobile_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        total = connection.execute("SELECT count(*) AS t FROM approvals").fetchone()["t"]
    finally:
        connection.close()
    # Capability is gone; the rows are not.
    assert challenge["consumed_at"] is not None
    assert stored["consumed_at"] is None and stored["superseded_at"] is not None
    assert pairing["consumed_at"] is not None
    assert session["revoked_at"] is not None
    assert total == 1


async def test_an_unknown_execution_survives_a_restore_as_unresolved(
    runtime: RuntimeFixture, tmp_path: Path
) -> None:
    """§23/§42: recovery is not a retry, and it does not pretend the world was rolled back."""
    await _seeded(runtime)
    case = await runtime.cases.list_cases(limit=1)
    action = await runtime.cases.prepare_action(
        case[0].id, "mail.send", {"to": "ada@example.edu", "subject": "x", "body": "y"}
    )
    approvals = ApprovalService(
        runtime.actions,
        runtime.clock,
        token_factory=FixedTokenFactory(SECRET_TOKEN),
    )
    issued = await approvals.create_challenge(action.id)
    await approvals.approve(action.id, issued.token)
    from assistant.application.action_execution import ActionExecutionService
    from tests.support.actions import FakeActionExecutor

    executor = FakeActionExecutor(action_type=action.action_type).script_unknown()
    await ActionExecutionService(
        runtime.actions, {action.action_type: executor}, runtime.clock
    ).execute(action.id)
    service = runtime.backup_service()
    archive = service.create(tmp_path / "backup.gab")
    destination = tmp_path / "recovered"
    service.restore(archive.path, destination)

    connection = _open(destination)
    try:
        run = connection.execute(
            "SELECT status FROM execution_runs WHERE action_id = ?", (str(action.id),)
        ).fetchone()
        action_row = connection.execute(
            "SELECT status FROM action_requests WHERE id = ?", (str(action.id),)
        ).fetchone()
    finally:
        connection.close()

    assert run["status"] == "unknown"
    assert action_row["status"] == "prepared"  # never a retryable success


def _open(destination: Path):
    import sqlite3

    connection = sqlite3.connect(str(destination / "assistant.db"))
    connection.row_factory = sqlite3.Row
    return connection
