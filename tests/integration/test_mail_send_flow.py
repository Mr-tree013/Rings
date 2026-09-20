"""Approved send end to end: prepare → approve → execute → reconcile (ADR-0024).

Real SQLite, the real Phase 6A approval and execution boundary, the real mail-send services, a
scripted SMTP executor and a scripted Sent mailbox. The properties under test are the ones a user
depends on when the message matters: the approved bytes are the sent bytes, a definite refusal is
not confused with an ambiguous drop, and nothing ever resends itself.
"""

from __future__ import annotations

import asyncio
import itertools
import threading
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from assistant.adapters.mail.smtp import SmtpMailExecutor, rfc2822_date
from assistant.application.action_execution import ActionExecutionService
from assistant.application.approval_service import ApprovalService
from assistant.application.case_service import CaseService
from assistant.application.mail_send_actions import MailSendActionService
from assistant.application.mail_send_reconciliation import (
    MailSendReconciliationService,
)
from assistant.application.mail_send_status import MailDeliveryState, MailSendStatusService
from assistant.domain.action import ActionRequest, ActionRequestStatus
from assistant.domain.approval import approval_ttl
from assistant.domain.config import MailAccountConfig
from assistant.domain.errors import (
    ActionExecutionUnresolved,
    ApprovalUnavailable,
    CapabilityUnavailable,
    CaseNotOpen,
    InvalidMailSend,
    MailConnectionError,
    MailCredentialsMissing,
    MailDraftNeedsUserInput,
    MailSendAlreadyPrepared,
    MailSendNotConfigured,
    MailSendNotReconcilable,
)
from assistant.domain.execution import ExecutionRunStatus
from assistant.domain.mail_send import MailSendPayload, MailSendReconciliationResult
from assistant.store.db import Database
from assistant.store.mail_send import SqliteMailSendRepository
from assistant.store.migrations import apply_migrations
from tests.support.actions import SECRET_TOKEN, ActionStores, FixedTokenFactory
from tests.support.fakes import FakeClock
from tests.support.mail_drafts import DraftStores
from tests.support.mail_intelligence import MailStores, build_message
from tests.support.mail_send import (
    FROM_ADDRESS,
    SEND_ACCOUNT_ID,
    SENT_MAILBOX,
    SMTP_PASSWORD,
    TO_ADDRESS,
    FakeSentLookup,
    StrictSmtpServer,
    build_sendable_draft,
    smtp_account,
)

NOW = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=NOW)


@pytest.fixture
def database(tmp_path: Path, clock: FakeClock) -> Database:
    db = Database.at(tmp_path / "assistant.db")
    apply_migrations(db, clock=clock)
    return db


@pytest.fixture
def stores(database: Database, clock: FakeClock) -> ActionStores:
    return ActionStores(database, clock)


@pytest.fixture
def drafts(database: Database, clock: FakeClock) -> DraftStores:
    return DraftStores(database, clock)


@pytest.fixture
def mail(database: Database, clock: FakeClock) -> MailStores:
    return MailStores(database, clock)


# ------------------------------------------------------------------------ helpers


async def _case(stores: ActionStores, clock: FakeClock, title: str = "Send the report"):
    return await CaseService(stores.cases, stores.actions, clock).create_case(title)


async def _draft_with_reply_target(
    drafts: DraftStores,
    mail: MailStores,
    *,
    needs_user_input: tuple[str, ...] = (),
    acknowledged: bool = False,
    version: int = 1,
):
    """A stored draft whose reply target exists, with the acknowledgement state asked for."""
    original = await mail.store(
        build_message(
            message_id_header="<original@example.edu>",
            from_address=TO_ADDRESS,
            subject="SE lab deadline",
            body_text="Could you submit the report by Friday?",
        )
    )
    draft = build_sendable_draft(needs_user_input=needs_user_input, version=version)
    draft = replace(draft, reply_to_message_id=original.id)
    if acknowledged:
        draft = draft.acknowledge_user_input(NOW)
    await drafts.drafts.add_draft(draft)
    return draft, original


def _random_message_id(domain: str) -> str:
    """A stand-in for the production factory: one fresh id per call.

    The real factory mints 128 random bits, and the link table makes a repeated id a database
    error — which is a guarantee under test, so a fixed test id would collide with it.
    """
    return f"<{uuid4().hex}@{domain}>"


def _send_service(
    stores: ActionStores,
    drafts: DraftStores,
    clock: FakeClock,
    *,
    accounts: tuple[MailAccountConfig, ...] | None = None,
    message_id_factory: object | None = None,
) -> MailSendActionService:
    factory = _random_message_id if message_id_factory is None else message_id_factory
    return MailSendActionService(
        drafts.drafts,
        drafts.mail,
        stores.cases,
        stores.actions,
        SqliteMailSendRepository(stores.database),
        clock,
        accounts=accounts if accounts is not None else (smtp_account(),),
        message_id_factory=factory,  # type: ignore[arg-type]
        date_header_factory=rfc2822_date,
    )


def _execution(
    stores: ActionStores, clock: FakeClock, *executors: object
) -> ActionExecutionService:
    return ActionExecutionService(
        stores.actions,
        {executor.action_type: executor for executor in executors},  # type: ignore[attr-defined]
        clock,
    )


def _smtp_executor(server: StrictSmtpServer, **overrides: object) -> SmtpMailExecutor:
    return SmtpMailExecutor(
        {SEND_ACCOUNT_ID: smtp_account(**overrides)},
        password_lookup=lambda account_id: SMTP_PASSWORD,
        client_factory=lambda target: server,
    )


_token_counter = itertools.count(1)


def _approval(stores: ActionStores, clock: FakeClock) -> ApprovalService:
    """An approval service whose tokens are recognisable and unique per call.

    The database refuses two challenges with the same token hash, so a repeated constant would be
    testing the wrong thing.
    """
    return ApprovalService(
        stores.actions,
        clock,
        token_factory=FixedTokenFactory(f"{SECRET_TOKEN}-{next(_token_counter)}"),
    )


async def _approve(stores: ActionStores, clock: FakeClock, action: ActionRequest) -> None:
    service = _approval(stores, clock)
    issued = await service.create_challenge(action.id)
    await service.approve(action.id, issued.token)


# ------------------------------------------------------------------- preparation


async def test_preparing_a_send_snapshots_the_exact_recipients_and_headers(
    stores: ActionStores, drafts: DraftStores, mail: MailStores, clock: FakeClock
) -> None:
    draft, original = await _draft_with_reply_target(drafts, mail)
    case = await _case(stores, clock)

    preparation = await _send_service(stores, drafts, clock).prepare_send(draft.id, case.id)

    payload = preparation.payload
    assert payload.draft_id == draft.id
    assert payload.draft_version == draft.version
    assert payload.from_address == FROM_ADDRESS
    assert payload.to_addresses == (TO_ADDRESS,)
    assert payload.subject == draft.subject
    assert payload.body_text == draft.body_text
    assert payload.rfc_message_id.startswith("<") and payload.rfc_message_id.endswith(
        "@example.edu>"
    )
    assert payload.in_reply_to_header == "<original@example.edu>"
    assert payload.references == ("<original@example.edu>",)
    action = preparation.action
    assert action.action_type.value == "mail.send"
    assert action.status is ActionRequestStatus.PREPARED
    assert action.payload["rfc_message_id"] == payload.rfc_message_id
    # The Message-ID is inside the fingerprint: what was approved includes it.
    assert action.fingerprint == action.fingerprint and action.fingerprint_matches()
    assert original.id == draft.reply_to_message_id


async def test_a_message_without_a_message_id_gets_no_threading_headers(
    stores: ActionStores, drafts: DraftStores, mail: MailStores, clock: FakeClock
) -> None:
    draft, _ = await _draft_with_reply_target(drafts, mail)
    original = await mail.mail.get_message(draft.reply_to_message_id)
    assert original is not None
    await mail.store(
        build_message(
            message_id_header=None, from_address=TO_ADDRESS, body_text="no id here"
        )
    )
    case = await _case(stores, clock)
    service = _send_service(stores, drafts, clock)
    prepared = await service.prepare_send(draft.id, case.id)
    assert prepared.payload.in_reply_to_header == "<original@example.edu>"

    # Now the same thing, but pointed at the message that has no Message-ID at all.
    without_id = await mail.store(
        build_message(message_id_header=None, from_address=TO_ADDRESS)
    )
    bare = replace(draft, id=uuid4(), reply_to_message_id=without_id.id, version=1)
    await drafts.drafts.add_draft(bare)

    bare_prepared = await service.prepare_send(bare.id, case.id)

    assert bare_prepared.payload.in_reply_to_header is None
    assert bare_prepared.payload.references == ()


async def test_preparing_needs_an_open_case(
    stores: ActionStores, drafts: DraftStores, mail: MailStores, clock: FakeClock
) -> None:
    draft, _ = await _draft_with_reply_target(drafts, mail)
    case = await _case(stores, clock)
    cases = CaseService(stores.cases, stores.actions, clock)
    await cases.complete_case(case.id)

    with pytest.raises(CaseNotOpen):
        await _send_service(stores, drafts, clock).prepare_send(draft.id, case.id)


async def test_preparing_needs_smtp_configuration(
    stores: ActionStores, drafts: DraftStores, mail: MailStores, clock: FakeClock
) -> None:
    draft, _ = await _draft_with_reply_target(drafts, mail)
    case = await _case(stores, clock)
    receive_only = MailAccountConfig(
        id=SEND_ACCOUNT_ID, host="imap.example.edu", username="s@example.edu", mailbox="INBOX"
    )

    with pytest.raises(MailSendNotConfigured):
        await _send_service(
            stores, drafts, clock, accounts=(receive_only,)
        ).prepare_send(draft.id, case.id)

    assert await stores.actions.count_actions() == 0


async def test_preparing_needs_no_smtp_credential(
    stores: ActionStores, drafts: DraftStores, mail: MailStores, clock: FakeClock
) -> None:
    """Prepare, review and approve first; the credential is only needed to execute."""
    draft, _ = await _draft_with_reply_target(drafts, mail)
    case = await _case(stores, clock)

    preparation = await _send_service(stores, drafts, clock).prepare_send(draft.id, case.id)

    assert preparation.action.is_prepared


async def test_one_draft_version_produces_one_action(
    stores: ActionStores, drafts: DraftStores, mail: MailStores, clock: FakeClock
) -> None:
    draft, _ = await _draft_with_reply_target(drafts, mail)
    case = await _case(stores, clock)
    service = _send_service(stores, drafts, clock)
    first = await service.prepare_send(draft.id, case.id)

    with pytest.raises(MailSendAlreadyPrepared):
        await service.prepare_send(draft.id, case.id)

    # Advancing the draft makes the next version sendable, through the same edit path a user uses.
    advanced = await drafts.service(None).edit_draft(draft.id, body="A revised note.")
    second = await service.prepare_send(advanced.id, case.id)

    assert second.action.id != first.action.id
    assert second.link.draft_version == 2


async def test_editing_a_draft_after_preparation_does_not_change_the_action(
    stores: ActionStores, drafts: DraftStores, mail: MailStores, clock: FakeClock
) -> None:
    """The approved bytes are frozen, and the CLI can tell that the draft moved on."""
    draft, _ = await _draft_with_reply_target(drafts, mail)
    case = await _case(stores, clock)
    preparation = await _send_service(stores, drafts, clock).prepare_send(draft.id, case.id)
    edited = replace(
        draft, version=draft.version + 1, body_text="Completely different words."
    )
    await drafts.drafts.update_draft(edited, expected_version=draft.version)

    stored = await stores.actions.get_action(preparation.action.id)
    status = await MailSendStatusService(
        stores.actions, SqliteMailSendRepository(stores.database), drafts.drafts, clock
    ).status(preparation.action.id)

    assert stored is not None and stored.payload["body_text"] == draft.body_text
    assert status.draft_version_changed is True
    assert status.current_draft_version == 2
    assert status.link.draft_version == 1


# ------------------------------------------------------- the needs-user-input gate


async def test_unacknowledged_questions_block_preparation(
    stores: ActionStores, drafts: DraftStores, mail: MailStores, clock: FakeClock
) -> None:
    draft, _ = await _draft_with_reply_target(
        drafts, mail, needs_user_input=("What is your student number?",)
    )
    case = await _case(stores, clock)

    with pytest.raises(MailDraftNeedsUserInput):
        await _send_service(stores, drafts, clock).prepare_send(draft.id, case.id)

    assert await stores.actions.count_actions() == 0


async def test_acknowledging_unblocks_preparation_and_an_edit_blocks_it_again(
    stores: ActionStores, drafts: DraftStores, mail: MailStores, clock: FakeClock
) -> None:
    draft, _ = await _draft_with_reply_target(
        drafts, mail, needs_user_input=("What is your student number?",)
    )
    case = await _case(stores, clock)
    service = _send_service(stores, drafts, clock)
    draft_service = drafts.service(None)

    acknowledged = await draft_service.acknowledge_user_input(draft.id)

    assert acknowledged.needs_user_input_acknowledged_at == NOW
    assert acknowledged.version == draft.version + 1
    assert acknowledged.needs_user_input == draft.needs_user_input  # kept for the record
    preparation = await service.prepare_send(acknowledged.id, case.id)
    assert preparation.link.draft_version == acknowledged.version

    # A later edit clears the acknowledgement, so the questions have to be re-read.
    edited = await draft_service.edit_draft(acknowledged.id, body="A new body.")
    assert edited.needs_user_input_acknowledged_at is None
    with pytest.raises(MailDraftNeedsUserInput):
        await service.prepare_send(edited.id, case.id)


# --------------------------------------------------------------------- execution


async def test_an_approved_send_is_delivered_exactly_once(
    stores: ActionStores, drafts: DraftStores, mail: MailStores, clock: FakeClock
) -> None:
    draft, _ = await _draft_with_reply_target(drafts, mail)
    case = await _case(stores, clock)
    preparation = await _send_service(stores, drafts, clock).prepare_send(draft.id, case.id)
    server = StrictSmtpServer()
    executor = _smtp_executor(server)
    assert executor.supports(preparation.action) is True
    await _approve(stores, clock, preparation.action)

    result = await _execution(stores, clock, executor).execute(preparation.action.id)

    assert result.status is ExecutionRunStatus.SUCCEEDED
    assert result.action_status == "executed"
    assert server.sent_bytes is not None
    payload = preparation.payload
    assert payload.body_text.encode() in server.sent_bytes
    assert server.stages.count("data") == 1
    stored = await stores.actions.get_action(preparation.action.id)
    assert stored is not None and stored.status is ActionRequestStatus.EXECUTED


async def test_the_sent_bytes_come_from_the_approved_payload_not_the_draft(
    stores: ActionStores, drafts: DraftStores, mail: MailStores, clock: FakeClock
) -> None:
    draft, _ = await _draft_with_reply_target(drafts, mail)
    case = await _case(stores, clock)
    preparation = await _send_service(stores, drafts, clock).prepare_send(draft.id, case.id)
    edited = replace(draft, version=draft.version + 1, body_text="Words the model never saw.")
    await drafts.drafts.update_draft(edited, expected_version=draft.version)
    server = StrictSmtpServer()
    await _approve(stores, clock, preparation.action)

    await _execution(stores, clock, _smtp_executor(server)).execute(preparation.action.id)

    assert server.sent_bytes is not None
    assert draft.body_text.encode() in server.sent_bytes
    assert b"Words the model never saw." not in server.sent_bytes


async def test_a_missing_credential_is_refused_before_the_approval_is_spent(
    stores: ActionStores, drafts: DraftStores, mail: MailStores, clock: FakeClock
) -> None:
    draft, _ = await _draft_with_reply_target(drafts, mail)
    case = await _case(stores, clock)
    preparation = await _send_service(stores, drafts, clock).prepare_send(draft.id, case.id)
    server = StrictSmtpServer()
    executor = SmtpMailExecutor(
        {SEND_ACCOUNT_ID: smtp_account()},
        password_lookup=lambda account_id: None,
        client_factory=lambda target: server,
    )
    await _approve(stores, clock, preparation.action)

    with pytest.raises(CapabilityUnavailable):
        await _execution(stores, clock, executor).execute(preparation.action.id)

    status = await MailSendStatusService(
        stores.actions, SqliteMailSendRepository(stores.database), drafts.drafts, clock
    ).status(preparation.action.id)
    assert status.approval_state.value == "valid"  # untouched
    assert status.execution is None
    assert server.calls == []


async def test_a_definite_refusal_spends_the_approval_and_needs_a_new_one(
    stores: ActionStores, drafts: DraftStores, mail: MailStores, clock: FakeClock
) -> None:
    draft, _ = await _draft_with_reply_target(drafts, mail)
    case = await _case(stores, clock)
    preparation = await _send_service(stores, drafts, clock).prepare_send(draft.id, case.id)
    server = StrictSmtpServer(fail_at="mail", error=_smtp_sender_refused())
    executor = _smtp_executor(server)
    await _approve(stores, clock, preparation.action)

    result = await _execution(stores, clock, executor).execute(preparation.action.id)

    assert result.status is ExecutionRunStatus.FAILED
    with pytest.raises(ApprovalUnavailable):
        await _execution(stores, clock, executor).execute(preparation.action.id)
    assert server.stages.count("data") == 0  # the body was never handed over


async def test_an_ambiguous_drop_leaves_the_attempt_unresolved_and_unretryable(
    stores: ActionStores, drafts: DraftStores, mail: MailStores, clock: FakeClock
) -> None:
    draft, _ = await _draft_with_reply_target(drafts, mail)
    case = await _case(stores, clock)
    preparation = await _send_service(stores, drafts, clock).prepare_send(draft.id, case.id)
    server = StrictSmtpServer(fail_at="data", error=_smtp_disconnected())
    executor = _smtp_executor(server)
    await _approve(stores, clock, preparation.action)

    result = await _execution(stores, clock, executor).execute(preparation.action.id)

    assert result.status is ExecutionRunStatus.UNKNOWN
    stored = await stores.actions.get_action(preparation.action.id)
    assert stored is not None and stored.status is ActionRequestStatus.PREPARED
    # Even a fresh approval cannot make it run again while the outcome is undecidable.
    await _approve(stores, clock, preparation.action)

    with pytest.raises(ActionExecutionUnresolved):
        await _execution(stores, clock, executor).execute(preparation.action.id)
    assert server.stages.count("data") == 1


async def test_two_concurrent_executions_still_send_one_message(
    stores: ActionStores, drafts: DraftStores, mail: MailStores, clock: FakeClock
) -> None:
    """One approval, one envelope: the second caller is refused while the first is mid-DATA."""
    draft, _ = await _draft_with_reply_target(drafts, mail)
    case = await _case(stores, clock)
    preparation = await _send_service(stores, drafts, clock).prepare_send(draft.id, case.id)
    server = BlockingSmtpServer()
    executor = _smtp_executor(server)
    await _approve(stores, clock, preparation.action)
    service = _execution(stores, clock, executor)

    first = asyncio.create_task(service.execute(preparation.action.id))
    await _wait_for_data(server)
    try:
        with pytest.raises(ActionExecutionUnresolved):
            await service.execute(preparation.action.id)
    finally:
        server.release.set()
    result = await first

    assert result.status is ExecutionRunStatus.SUCCEEDED
    assert server.stages.count("data") == 1
    assert await stores.actions.count_executions(preparation.action.id) == 1


async def _wait_for_data(server: BlockingSmtpServer) -> None:
    """Wait until the first attempt is inside `DATA`."""
    for _ in range(500):
        if server.data_entered.is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the first attempt never reached DATA")


def _smtp_sender_refused() -> Exception:
    import smtplib

    return smtplib.SMTPSenderRefused(550, b"no such sender", FROM_ADDRESS)


def _smtp_disconnected() -> Exception:
    import smtplib

    return smtplib.SMTPServerDisconnected("connection dropped")


@dataclass
class BlockingSmtpServer(StrictSmtpServer):
    """A server that holds `DATA` open until the test releases it.

    The executor runs in a worker thread, so the gate is a `threading.Event` rather than an
    asyncio one: the point is to hold a real SMTP conversation open while a second caller races it.
    """

    data_entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)

    def data(self, message: bytes | str) -> tuple[int, bytes]:
        payload = message.encode("utf-8") if isinstance(message, str) else message
        self.calls.append(("data", payload))
        self.data_entered.set()
        self.release.wait(timeout=10)
        return 250, b"queued"


# ------------------------------------------------------------------ reconciliation


async def _unresolved_send(
    stores: ActionStores, drafts: DraftStores, mail: MailStores, clock: FakeClock
):
    """A send whose attempt ended ambiguously: the state reconciliation exists for."""
    draft, _ = await _draft_with_reply_target(drafts, mail)
    case = await _case(stores, clock)
    preparation = await _send_service(stores, drafts, clock).prepare_send(draft.id, case.id)
    server = StrictSmtpServer(fail_at="data", error=_smtp_disconnected())
    executor = _smtp_executor(server)
    await _approve(stores, clock, preparation.action)
    result = await _execution(stores, clock, executor).execute(preparation.action.id)
    assert result.status is ExecutionRunStatus.UNKNOWN
    return preparation, server


def _reconciler(
    stores: ActionStores,
    clock: FakeClock,
    lookup: FakeSentLookup | None,
    *,
    accounts: tuple[MailAccountConfig, ...] | None = None,
) -> MailSendReconciliationService:
    return MailSendReconciliationService(
        stores.actions,
        SqliteMailSendRepository(stores.database),
        clock,
        lookup=lookup,
        accounts=accounts if accounts is not None else (smtp_account(),),
    )


async def test_finding_the_exact_message_resolves_the_attempt(
    stores: ActionStores, drafts: DraftStores, mail: MailStores, clock: FakeClock
) -> None:
    preparation, server = await _unresolved_send(stores, drafts, mail, clock)
    lookup = FakeSentLookup().with_match(uid=42, uidvalidity=7)

    outcome = await _reconciler(stores, clock, lookup).reconcile(preparation.action.id)

    assert outcome.result is MailSendReconciliationResult.FOUND
    assert outcome.resolved is True
    assert outcome.reconciliation.mailbox_name == SENT_MAILBOX
    assert outcome.reconciliation.uid == 42
    assert outcome.reconciliation.uidvalidity == 7
    stored = await stores.actions.get_action(preparation.action.id)
    assert stored is not None and stored.status is ActionRequestStatus.EXECUTED
    run = await stores.actions.latest_execution(preparation.action.id)
    assert run is not None and run.status is ExecutionRunStatus.SUCCEEDED
    # The lookup searched for the approved id, in the configured Sent mailbox.
    assert lookup.answered == [(SENT_MAILBOX, preparation.payload.rfc_message_id)]
    assert server.stages.count("data") == 1  # reconciliation never sends


async def test_a_confirmed_send_is_idempotent(
    stores: ActionStores, drafts: DraftStores, mail: MailStores, clock: FakeClock
) -> None:
    preparation, _ = await _unresolved_send(stores, drafts, mail, clock)
    lookup = FakeSentLookup().with_match()
    service = _reconciler(stores, clock, lookup)
    await service.reconcile(preparation.action.id)

    again = await service.reconcile(preparation.action.id)

    assert again.already_sent is True
    assert len(lookup.answered) == 1  # no second mailbox read either


async def test_not_found_proves_nothing_and_changes_nothing(
    stores: ActionStores, drafts: DraftStores, mail: MailStores, clock: FakeClock
) -> None:
    preparation, _ = await _unresolved_send(stores, drafts, mail, clock)
    lookup = FakeSentLookup().with_nothing()

    outcome = await _reconciler(stores, clock, lookup).reconcile(preparation.action.id)

    assert outcome.result is MailSendReconciliationResult.NOT_FOUND
    assert outcome.resolved is False
    stored = await stores.actions.get_action(preparation.action.id)
    assert stored is not None and stored.status is ActionRequestStatus.PREPARED
    run = await stores.actions.latest_execution(preparation.action.id)
    assert run is not None and run.status is ExecutionRunStatus.UNKNOWN
    history = await SqliteMailSendRepository(stores.database).list_reconciliations(
        preparation.action.id
    )
    assert [row.result for row in history] == [MailSendReconciliationResult.NOT_FOUND]
    assert await stores.actions.list_approvals(preparation.action.id)  # unchanged history


async def test_an_ambiguous_lookup_stays_unresolved(
    stores: ActionStores, drafts: DraftStores, mail: MailStores, clock: FakeClock
) -> None:
    preparation, _ = await _unresolved_send(stores, drafts, mail, clock)
    lookup = FakeSentLookup().with_ambiguity()

    outcome = await _reconciler(stores, clock, lookup).reconcile(preparation.action.id)

    assert outcome.result is MailSendReconciliationResult.AMBIGUOUS
    assert outcome.reconciliation.uid is None  # nobody chose between the two UIDs
    stored = await stores.actions.get_action(preparation.action.id)
    assert stored is not None and stored.status is ActionRequestStatus.PREPARED


@pytest.mark.parametrize(
    "lookup",
    [
        FakeSentLookup().with_error(MailCredentialsMissing("no credential")),
        FakeSentLookup().with_error(MailConnectionError("connection refused")),
    ],
)
async def test_an_unusable_mailbox_is_recorded_as_unavailable(
    stores: ActionStores,
    drafts: DraftStores,
    mail: MailStores,
    clock: FakeClock,
    lookup: FakeSentLookup,
) -> None:
    preparation, _ = await _unresolved_send(stores, drafts, mail, clock)

    outcome = await _reconciler(stores, clock, lookup).reconcile(preparation.action.id)

    assert outcome.result is MailSendReconciliationResult.UNAVAILABLE
    stored = await stores.actions.get_action(preparation.action.id)
    assert stored is not None and stored.status is ActionRequestStatus.PREPARED
    run = await stores.actions.latest_execution(preparation.action.id)
    assert run is not None and run.status is ExecutionRunStatus.UNKNOWN


async def test_no_lookup_at_all_is_unavailable(
    stores: ActionStores, drafts: DraftStores, mail: MailStores, clock: FakeClock
) -> None:
    preparation, _ = await _unresolved_send(stores, drafts, mail, clock)

    outcome = await _reconciler(stores, clock, None).reconcile(preparation.action.id)

    assert outcome.result is MailSendReconciliationResult.UNAVAILABLE


async def test_reconciliation_needs_an_unresolved_attempt(
    stores: ActionStores, drafts: DraftStores, mail: MailStores, clock: FakeClock
) -> None:
    draft, _ = await _draft_with_reply_target(drafts, mail)
    case = await _case(stores, clock)
    preparation = await _send_service(stores, drafts, clock).prepare_send(draft.id, case.id)
    service = _reconciler(stores, clock, FakeSentLookup().with_match())

    with pytest.raises(MailSendNotReconcilable):
        await service.reconcile(preparation.action.id)  # nothing was ever attempted

    server = StrictSmtpServer()
    executor = _smtp_executor(server)
    await _approve(stores, clock, preparation.action)
    await _execution(stores, clock, executor).execute(preparation.action.id)
    # A resolved attempt needs no lookup at all: the answer is already known.
    settled = await service.reconcile(preparation.action.id)
    assert settled.already_sent is True
    assert settled.result is MailSendReconciliationResult.FOUND


async def test_the_lookup_uses_the_approved_message_id_not_a_reconstructed_one(
    stores: ActionStores, drafts: DraftStores, mail: MailStores, clock: FakeClock
) -> None:
    preparation, _ = await _unresolved_send(stores, drafts, mail, clock)
    lookup = FakeSentLookup().with_nothing()

    await _reconciler(stores, clock, lookup).reconcile(preparation.action.id)

    searched = lookup.answered[0][1]
    assert searched == preparation.payload.rfc_message_id
    assert searched == preparation.link.rfc_message_id
    assert searched.startswith("<") and searched.endswith(">")


# ------------------------------------------------------------------------- status


async def test_the_delivery_state_follows_the_records(
    stores: ActionStores, drafts: DraftStores, mail: MailStores, clock: FakeClock
) -> None:
    draft, _ = await _draft_with_reply_target(drafts, mail)
    case = await _case(stores, clock)
    preparation = await _send_service(stores, drafts, clock).prepare_send(draft.id, case.id)
    service = MailSendStatusService(
        stores.actions, SqliteMailSendRepository(stores.database), drafts.drafts, clock
    )

    assert (await service.status(preparation.action.id)).state is MailDeliveryState.DRAFT

    await _approve(stores, clock, preparation.action)
    assert (await service.status(preparation.action.id)).state is MailDeliveryState.APPROVED

    clock.advance(approval_ttl().total_seconds() + 1)
    assert (await service.status(preparation.action.id)).state is MailDeliveryState.DRAFT


async def test_an_unsuccessful_attempt_reads_as_failed_and_an_unknown_one_as_unknown(
    stores: ActionStores, drafts: DraftStores, mail: MailStores, clock: FakeClock
) -> None:
    draft, _ = await _draft_with_reply_target(drafts, mail)
    case = await _case(stores, clock)
    preparation = await _send_service(stores, drafts, clock).prepare_send(draft.id, case.id)
    status_service = MailSendStatusService(
        stores.actions, SqliteMailSendRepository(stores.database), drafts.drafts, clock
    )
    await _approve(stores, clock, preparation.action)
    refusing = StrictSmtpServer(fail_at="mail", error=_smtp_sender_refused())
    failed = await _execution(
        stores, clock, _smtp_executor(refusing)
    ).execute(preparation.action.id)
    assert failed.status is ExecutionRunStatus.FAILED
    assert (await status_service.status(preparation.action.id)).state is MailDeliveryState.FAILED

    preparation2, _ = await _unresolved_send(stores, drafts, mail, clock)
    assert (
        await status_service.status(preparation2.action.id)
    ).state is MailDeliveryState.SENDING_UNKNOWN


async def test_the_send_list_only_shows_prepared_sends(
    stores: ActionStores, drafts: DraftStores, mail: MailStores, clock: FakeClock
) -> None:
    other_case = await _case(stores, clock, "Something else")
    await CaseService(stores.cases, stores.actions, clock).prepare_action(
        other_case.id, "ehall.submit-certificate", {"form": "x"}
    )
    draft, _ = await _draft_with_reply_target(drafts, mail)
    case = await _case(stores, clock)
    preparation = await _send_service(stores, drafts, clock).prepare_send(draft.id, case.id)
    service = MailSendStatusService(
        stores.actions, SqliteMailSendRepository(stores.database), drafts.drafts, clock
    )

    listed = await service.list_statuses(limit=None)

    assert [status.action.id for status in listed] == [preparation.action.id]


@pytest.mark.parametrize(
    "message_id",
    ["not-a-message-id", "<no-domain>", "<a b@example.edu>", ""],
)
def test_an_unusable_message_id_can_never_be_prepared(message_id: str) -> None:
    """A malformed identifier would break the Sent lookup and the header block alike."""
    with pytest.raises(InvalidMailSend):
        MailSendPayload(
            draft_id=uuid4(),
            draft_version=1,
            account_id=SEND_ACCOUNT_ID,
            from_address=FROM_ADDRESS,
            to_addresses=(TO_ADDRESS,),
            subject="Re: x",
            body_text="body",
            rfc_message_id=message_id,
            date_header="Tue, 22 Sep 2026 09:00:00 +0000",
        )


__all__ = ["NOW"]
