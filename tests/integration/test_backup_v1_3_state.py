"""Backup and restore over the v1.3 durable state (Phase 11F; ADR-0042/0043/0044/0045).

The archive format did not change in v1.3, and neither did the rule that governs it: a snapshot
carries durable *state*, never authority. These tests prove both halves for what this release
added — attention, planning preferences, superseded plan blocks and the generalized external
review — and prove the absences an operator depends on: no credential, no eHall profile, no
session, no stream state, and no approval that survives a restore.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from assistant.adapters.ops.content_objects import RuntimeContentObjects
from assistant.application.approval_service import ApprovalService
from assistant.application.integrity_service import IntegrityService
from assistant.domain.action import ActionRequest, ActionType
from assistant.domain.attention import (
    AttentionItem,
    AttentionKind,
    AttentionSeverity,
    AttentionSourceType,
    AttentionStatus,
)
from assistant.domain.case import Case
from assistant.domain.conversation import (
    ConversationMessage,
    ConversationMessageRole,
    ConversationOperation,
    ConversationThread,
    ConversationTurn,
)
from assistant.domain.conversation_plan import ConversationOperationType, build_arguments
from assistant.domain.conversation_review import ConversationExternalReview
from assistant.domain.ehall import (
    EHallCertificatePayload,
    build_field_values,
)
from assistant.domain.planning_preferences import PlanningPreferences
from assistant.store.actions import SqliteActionRepository
from assistant.store.attention import SqliteAttentionRepository
from assistant.store.cases import SqliteCaseRepository
from assistant.store.conversation_reviews import SqliteConversationReviewRepository
from assistant.store.conversations import SqliteConversationRepository
from assistant.store.db import Database
from assistant.store.integrity import SqliteIntegrityRepository
from assistant.store.planning_preferences import SqlitePlanningPreferencesRepository
from tests.support.actions import SECRET_TOKEN, FixedTokenFactory
from tests.support.ehall import certificate_snapshot
from tests.support.ops import NOW, RuntimeFixture

APPLICANT = "张三"
CERTIFICATE_TYPE = "在读证明"
REPLACED_PROPOSAL_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def runtime(tmp_path: Path) -> RuntimeFixture:
    return RuntimeFixture(tmp_path / "data" / "growing-assistant")


def _open(destination: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(destination / "assistant.db"))
    connection.row_factory = sqlite3.Row
    return connection


async def _seed_attention(
    runtime: RuntimeFixture, *, key: str, acknowledged: bool
) -> UUID:
    """One attention row, optionally settled, written through the real store."""
    repository = SqliteAttentionRepository(runtime.database)
    now = runtime.clock.now()
    stored = await repository.add_item(
        AttentionItem(
            kind=AttentionKind.EXTERNAL_REVIEW_WAITING,
            source_type=AttentionSourceType.CONVERSATION_REVIEW,
            source_id=f"review-{key}",
            dedupe_key=f"review:{key}",
            fingerprint="a" * 64,
            severity=AttentionSeverity.HIGH,
            title="有一项学校系统的操作等待你确认",
            summary="只有你亲自确认之后才会执行。",
            created_at=now,
            updated_at=now,
        )
    )
    if acknowledged:
        stored = await repository.update_item(stored.acknowledge(runtime.clock.now()))
    return stored.id


async def _seed_preferences(runtime: RuntimeFixture) -> None:
    now = runtime.clock.now()
    await SqlitePlanningPreferencesRepository(runtime.database, runtime.clock).save_preferences(
        PlanningPreferences(
            day_start_local=8 * 60,
            day_end_local=22 * 60,
            max_daily_minutes=360,
            preferred_block_minutes=50,
            max_block_minutes=120,
            created_at=now,
            updated_at=now,
        )
    )


async def _seed_superseded_block(runtime: RuntimeFixture) -> str:
    """One cancelled planner block that a later proposal replaced.

    Written directly because both facts are store-level: `apply_proposal` writes the supersession
    columns inside its transaction (ADR-0044 §35), and this test is about what a backup carries.
    """
    task = await runtime.add_task("Write the report")
    block_id = str(uuid4())
    stamp = NOW.isoformat(timespec="microseconds")
    with runtime.database.connect() as connection:
        connection.execute(
            "INSERT INTO plan_proposals (id, status, window_start, window_end, timezone, "
            "input_fingerprint, input_revision, created_at, applied_at, superseded_at) VALUES "
            "(?, 'applied', ?, ?, 'Asia/Shanghai', ?, 0, ?, ?, NULL)",
            (
                REPLACED_PROPOSAL_ID,
                NOW.isoformat(timespec="microseconds"),
                NOW.replace(hour=18).isoformat(timespec="microseconds"),
                "f" * 64,
                NOW.isoformat(timespec="microseconds"),
                NOW.isoformat(timespec="microseconds"),
            ),
        )
        connection.execute(
            "INSERT INTO plan_blocks (id, task_id, starts_at, ends_at, created_at, updated_at, "
            "cancelled_at, origin, proposal_id, superseded_at, superseded_by_proposal_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'planner', ?, ?, ?)",
            (
                block_id,
                str(task.id),
                stamp,
                NOW.replace(hour=10).isoformat(timespec="microseconds"),
                stamp,
                stamp,
                stamp,
                REPLACED_PROPOSAL_ID,
                stamp,
                REPLACED_PROPOSAL_ID,
            ),
        )
    return block_id


async def _seed_waiting_certificate_review(runtime: RuntimeFixture) -> tuple[UUID, UUID]:
    """A prepared certificate action plus the waiting review that points at it.

    Written through the real stores, so the restored copy is audited against a review the product
    itself would have produced.
    """
    conversations = SqliteConversationRepository(runtime.database)
    reviews = SqliteConversationReviewRepository(runtime.database)
    actions = SqliteActionRepository(runtime.database)
    now = runtime.clock.now()
    thread = await conversations.add_thread(ConversationThread(created_at=now, updated_at=now))
    message = await conversations.add_message(
        ConversationMessage(
            thread_id=thread.id,
            role=ConversationMessageRole.USER,
            text=f"帮我申请{CERTIFICATE_TYPE}，申请人姓名是{APPLICANT}，证明书类型是{CERTIFICATE_TYPE}。",
            created_at=now,
        )
    )
    turn = await conversations.add_turn(
        ConversationTurn(
            thread_id=thread.id,
            user_message_id=message.id,
            interpreter_version="test",
            context_fingerprint="f" * 64,
            created_at=now,
        )
    )
    operation = await conversations.add_operation(
        ConversationOperation(
            turn_id=turn.id,
            ordinal=0,
            operation_type=ConversationOperationType.EHALL_CERTIFICATE_PREPARE,
            arguments=build_arguments(
                "ehall.certificate.prepare",
                {
                    "fields": {
                        "applicant-name": APPLICANT,
                        "certificate-type": CERTIFICATE_TYPE,
                    },
                    "case_id": None,
                },
            ),
            operation_fingerprint="e" * 64,
            created_at=now,
            updated_at=now,
        )
    )
    case = await SqliteCaseRepository(runtime.database).add_case(
        Case(title="eHall certificate", created_at=now, updated_at=now)
    )
    snapshot = certificate_snapshot()
    certificate = EHallCertificatePayload(
        fields=build_field_values(
            snapshot,
            {"applicant-name": APPLICANT, "certificate-type": CERTIFICATE_TYPE},
        ),
        page_contract_fingerprint=snapshot.fingerprint(),
        required_materials=snapshot.required_materials,
    )
    action = await actions.add_action(
        ActionRequest.prepare(
            case_id=case.id,
            action_type=ActionType("ehall.submit-certificate"),
            payload=certificate.to_payload(),
            at=now,
        )
    )
    review = await reviews.add_review(
        ConversationExternalReview(
            conversation_operation_id=operation.id,
            thread_id=thread.id,
            action_request_id=action.id,
            action_type="ehall.submit-certificate",
            action_fingerprint=action.fingerprint,
            expires_at=now + timedelta(minutes=30),
            created_at=now,
            updated_at=now,
        )
    )
    return review.id, action.id


async def test_attention_state_survives_a_backup_and_a_restore(
    runtime: RuntimeFixture, tmp_path: Path
) -> None:
    """An inbox that comes back unread after a restore is an inbox nobody can trust."""
    open_id = await _seed_attention(runtime, key="open", acknowledged=False)
    settled_id = await _seed_attention(runtime, key="settled", acknowledged=True)
    service = runtime.backup_service()
    archive = service.create(tmp_path / "backup.gab")
    destination = tmp_path / "recovered"
    service.restore(archive.path, destination)

    restored = SqliteAttentionRepository(Database.at(destination / "assistant.db"))
    open_item = await restored.get_item(open_id)
    settled_item = await restored.get_item(settled_id)

    assert open_item is not None and open_item.status is AttentionStatus.OPEN
    assert settled_item is not None
    assert settled_item.status is AttentionStatus.ACKNOWLEDGED
    assert settled_item.acknowledged_at is not None


async def test_planning_preferences_and_superseded_blocks_survive_a_restore(
    runtime: RuntimeFixture, tmp_path: Path
) -> None:
    """Capacity is a preference and a replaced block is history: both are durable state."""
    await _seed_preferences(runtime)
    block_id = await _seed_superseded_block(runtime)
    service = runtime.backup_service()
    archive = service.create(tmp_path / "backup.gab")
    destination = tmp_path / "recovered"
    service.restore(archive.path, destination)

    connection = _open(destination)
    try:
        preferences = connection.execute(
            "SELECT day_end_local, max_daily_minutes, preferred_block_minutes FROM "
            "planning_preferences WHERE id = 'current'"
        ).fetchone()
        block = connection.execute(
            "SELECT cancelled_at, superseded_at, superseded_by_proposal_id FROM plan_blocks "
            "WHERE id = ?",
            (block_id,),
        ).fetchone()
    finally:
        connection.close()

    assert tuple(preferences) == (22 * 60, 360, 50)
    assert block["cancelled_at"] is not None
    assert block["superseded_at"] is not None
    assert block["superseded_by_proposal_id"] == REPLACED_PROPOSAL_ID


async def test_a_waiting_certificate_review_survives_and_still_audits_clean(
    runtime: RuntimeFixture, tmp_path: Path
) -> None:
    """The generalized review row is durable state, and the restored copy passes the audit."""
    review_id, action_id = await _seed_waiting_certificate_review(runtime)
    service = runtime.backup_service()
    archive = service.create(tmp_path / "backup.gab")
    destination = tmp_path / "recovered"
    result = service.restore(archive.path, destination)

    connection = _open(destination)
    try:
        review = connection.execute(
            "SELECT action_type, status, action_request_id, action_fingerprint "
            "FROM conversation_external_reviews WHERE id = ?",
            (str(review_id),),
        ).fetchone()
        approvals = connection.execute("SELECT count(*) AS t FROM approvals").fetchone()["t"]
        runs = connection.execute("SELECT count(*) AS t FROM execution_runs").fetchone()["t"]
    finally:
        connection.close()

    assert review["action_type"] == "ehall.submit-certificate"
    assert review["status"] == "waiting"
    assert review["action_request_id"] == str(action_id)
    assert len(review["action_fingerprint"]) == 64
    # Nothing was approved and nothing ran before the backup, and nothing is invented now.
    assert approvals == 0
    assert runs == 0
    assert result.approvals_superseded == 0

    report = await IntegrityService(
        SqliteIntegrityRepository(Database.at(destination / "assistant.db")),
        RuntimeContentObjects(destination),
    ).check()
    section = next(item for item in report.sections if item.name == "external_reviews")
    assert section.severity.value == "ok", section.findings


async def test_a_restore_never_recreates_approval_authority_for_a_review(
    runtime: RuntimeFixture, tmp_path: Path
) -> None:
    """An outstanding approval is superseded by a restore, never handed back as authority."""
    review_id, action_id = await _seed_waiting_certificate_review(runtime)
    approvals = ApprovalService(
        runtime.actions, runtime.clock, token_factory=FixedTokenFactory(SECRET_TOKEN)
    )
    issued = await approvals.create_challenge(action_id)
    approved = await approvals.approve(action_id, issued.token)
    service = runtime.backup_service()
    archive = service.create(tmp_path / "backup.gab")
    destination = tmp_path / "recovered"
    result = service.restore(archive.path, destination)

    connection = _open(destination)
    try:
        stored = connection.execute(
            "SELECT consumed_at, superseded_at FROM approvals WHERE id = ?",
            (str(approved.id),),
        ).fetchone()
        review = connection.execute(
            "SELECT status FROM conversation_external_reviews WHERE id = ?",
            (str(review_id),),
        ).fetchone()
    finally:
        connection.close()

    assert result.approvals_superseded == 1
    assert stored["consumed_at"] is None and stored["superseded_at"] is not None
    # The review is still a pointer, not a permission: it has to be confirmed again, by hand.
    assert review["status"] == "waiting"


async def test_the_archive_carries_no_stream_state_credential_or_ehall_profile(
    runtime: RuntimeFixture, tmp_path: Path
) -> None:
    """§12: derived caches, secrets and the browser profile have no member and no column."""
    await _seed_waiting_certificate_review(runtime)
    profile = runtime.runtime / "ehall" / "nju-profile"
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "Cookies").write_text("SENTINEL-EHALL-COOKIE", encoding="utf-8")
    (runtime.runtime / ".env").write_text("SENTINEL-MAIL-PASSWORD", encoding="utf-8")

    service = runtime.backup_service()
    archive = service.create(tmp_path / "backup.gab")
    handle = service._archive.read(archive.path)

    assert set(handle.member_names) == {"manifest.json", "runtime.sqlite3"}
    blob = archive.manifest.to_json()
    for forbidden in ("Cookies", ".env", "SENTINEL", "nju-profile"):
        assert forbidden not in blob

    connection = _open_snapshot_member(handle, tmp_path)
    try:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        columns = {
            f"{table}.{row[1]}"
            for table in tables
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
    finally:
        connection.close()

    # An SSE stream is in-process by construction, so there is no durable state to archive.
    assert not [name for name in tables if "stream" in name or name.startswith("sse")]
    # No credential column exists anywhere in the runtime schema.
    assert not [
        name
        for name in columns
        if "password" in name.lower() or "secret" in name.lower()
        if "hash" not in name.lower()
    ]
    # No credential-shaped column exists at all. (A lease *fencing* token is not a credential, and
    # the pairing, session and approval capabilities are stored as hashes, which the next check
    # pins: this is the schema's own promise, not a naming convention.)
    forbidden_columns = {
        "api_key",
        "apikey",
        "cookie",
        "credentials",
        "password",
        "private_key",
        "secret",
        "token",
    }
    assert not [
        name for name in columns if name.rsplit(".", 1)[-1].lower() in forbidden_columns
    ]
    assert not [name for name in columns if name.lower().endswith("_password")]


def test_the_manifest_names_the_release_it_was_built_from(
    runtime: RuntimeFixture, tmp_path: Path
) -> None:
    archive = runtime.backup_service().create(tmp_path / "backup.gab")

    assert archive.manifest.migration_files[-1] == "0023_conversation_review_expansion.sql"
    assert archive.manifest.application_version == "0.8.0"


def _open_snapshot_member(handle: object, tmp_path: Path) -> sqlite3.Connection:
    """Read the archive's database member without touching the live runtime."""
    member = next(
        name for name in handle.member_names if name.endswith(".sqlite3")  # type: ignore[attr-defined]
    )
    target = tmp_path / f"archive-copy-{uuid4().hex}.db"
    handle.extract(member, target)  # type: ignore[attr-defined]
    connection = sqlite3.connect(str(target))
    connection.row_factory = sqlite3.Row
    return connection
