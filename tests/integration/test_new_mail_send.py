"""The new-mail half of the approved-send boundary (ADR-0037 §7, §21, §26).

Real SQLite, the real approval boundary, the real `mail.send` preparation, a scripted SMTP server
and a scripted Sent mailbox. What is under test is that a *new* letter converges on exactly the
same pipeline as a reply — one action type, one Message-ID, one link table, one executor — while
the payload it approves never claims to answer anything.
"""

from __future__ import annotations

import email
import itertools
from dataclasses import replace
from datetime import UTC, datetime
from email.header import decode_header
from pathlib import Path
from uuid import uuid4

import pytest

from assistant.adapters.mail.smtp import SmtpMailExecutor, build_rfc822_message, rfc2822_date
from assistant.application.action_execution import ActionExecutionService
from assistant.application.approval_service import ApprovalService
from assistant.application.case_service import CaseService
from assistant.application.mail_send_actions import MailSendActionService
from assistant.application.mail_send_reconciliation import MailSendReconciliationService
from assistant.application.mail_send_status import MailDeliveryState, MailSendStatusService
from assistant.domain.action import ActionRequest, ActionRequestStatus
from assistant.domain.config import MailAccountConfig
from assistant.domain.errors import (
    InvalidMailSend,
    InvalidNewMailDraft,
    MailSendAlreadyPrepared,
)
from assistant.domain.execution import ExecutionRunStatus
from assistant.domain.mail_send import MailSendKind, MailSendPayload
from assistant.domain.new_mail_draft import NewMailDraft
from assistant.store.db import Database
from assistant.store.mail import SqliteMailRepository
from assistant.store.mail_drafts import SqliteMailDraftRepository
from assistant.store.mail_send import SqliteMailSendRepository
from assistant.store.migrations import apply_migrations
from assistant.store.new_mail_drafts import SqliteNewMailDraftRepository
from tests.support.actions import SECRET_TOKEN, ActionStores, FixedTokenFactory
from tests.support.fakes import FakeClock
from tests.support.mail_send import (
    FROM_ADDRESS,
    SEND_ACCOUNT_ID,
    SENT_MAILBOX,
    SMTP_PASSWORD,
    TO_ADDRESS,
    FakeSentLookup,
    StrictSmtpServer,
    smtp_account,
)

NOW = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
SUBJECT = "测试"
BODY = "你好，这是一封新邮件。"


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
def drafts(database: Database) -> SqliteNewMailDraftRepository:
    return SqliteNewMailDraftRepository(database)


async def _new_draft(
    drafts: SqliteNewMailDraftRepository,
    *,
    version: int = 1,
    to_address: str = TO_ADDRESS,
    subject: str = SUBJECT,
    body_text: str = BODY,
) -> NewMailDraft:
    draft = NewMailDraft(
        account_id=SEND_ACCOUNT_ID,
        to_address=to_address,
        subject=subject,
        body_text=body_text,
        version=version,
        created_at=NOW,
        updated_at=NOW,
    )
    return await drafts.add_draft(draft)


def _random_message_id(domain: str) -> str:
    return f"<{uuid4().hex}@{domain}>"


def _send_service(
    stores: ActionStores,
    drafts: SqliteNewMailDraftRepository,
    clock: FakeClock,
    *,
    accounts: tuple[MailAccountConfig, ...] | None = None,
) -> MailSendActionService:
    return MailSendActionService(
        SqliteMailDraftRepository(stores.database),
        SqliteMailRepository(stores.database),
        stores.cases,
        stores.actions,
        SqliteMailSendRepository(stores.database),
        clock,
        accounts=accounts if accounts is not None else (smtp_account(),),
        message_id_factory=_random_message_id,
        date_header_factory=rfc2822_date,
        new_drafts=drafts,
    )


async def _case(stores: ActionStores, clock: FakeClock, title: str = "Send a new letter"):
    return await CaseService(stores.cases, stores.actions, clock).create_case(title)


_token_counter = itertools.count(1)


async def _approve(stores: ActionStores, clock: FakeClock, action: ActionRequest) -> None:
    service = ApprovalService(
        stores.actions,
        clock,
        token_factory=FixedTokenFactory(f"{SECRET_TOKEN}-{next(_token_counter)}"),
    )
    issued = await service.create_challenge(action.id)
    await service.approve(action.id, issued.token)


def _smtp_executor(server: StrictSmtpServer) -> SmtpMailExecutor:
    return SmtpMailExecutor(
        {SEND_ACCOUNT_ID: smtp_account()},
        password_lookup=lambda account_id: SMTP_PASSWORD,
        client_factory=lambda target: server,
    )


def _execution(
    stores: ActionStores, clock: FakeClock, *executors: SmtpMailExecutor
) -> ActionExecutionService:
    """The real execution boundary over the scripted transport, keyed by action type."""
    return ActionExecutionService(
        stores.actions,
        {executor.action_type: executor for executor in executors},
        clock,
    )


def _decoded_subject(raw: bytes) -> str:
    """The Subject of the transmitted bytes, decoded from its RFC 2047 form."""
    message = email.message_from_bytes(raw)
    decoded = decode_header(message["Subject"])[0][0]
    return decoded.decode() if isinstance(decoded, bytes) else decoded


# ----------------------------------------------------------------------- preparation


async def test_preparing_a_new_letter_snapshots_the_exact_content(
    stores: ActionStores, drafts: SqliteNewMailDraftRepository, clock: FakeClock
) -> None:
    draft = await _new_draft(drafts)
    case = await _case(stores, clock)

    preparation = await _send_service(stores, drafts, clock).prepare_new_send(draft.id, case.id)

    payload = preparation.payload
    assert payload.kind is MailSendKind.NEW
    assert payload.from_address == FROM_ADDRESS
    assert payload.to_addresses == (TO_ADDRESS,)
    assert payload.subject == SUBJECT
    assert payload.body_text == BODY
    assert payload.in_reply_to_header is None
    assert payload.references == ()
    assert preparation.link.new_draft_id == draft.id
    assert preparation.link.draft_id is None
    assert preparation.link.kind is MailSendKind.NEW
    # The action itself says which kind of letter it is, without the link.
    assert preparation.action.payload["kind"] == "new"
    assert preparation.action.payload["in_reply_to_header"] is None


async def test_one_draft_version_produces_exactly_one_action(
    stores: ActionStores, drafts: SqliteNewMailDraftRepository, clock: FakeClock
) -> None:
    draft = await _new_draft(drafts)
    case = await _case(stores, clock)
    service = _send_service(stores, drafts, clock)
    first = await service.prepare_new_send(draft.id, case.id)

    with pytest.raises(MailSendAlreadyPrepared):
        await service.prepare_new_send(draft.id, case.id)

    revised = draft.revised(at=NOW, body_text="改过的正文")
    await drafts.update_draft(revised, expected_version=draft.version)
    second = await service.prepare_new_send(draft.id, case.id)

    assert second.action.id != first.action.id
    assert second.action.fingerprint != first.action.fingerprint
    assert second.payload.draft_version == 2
    assert second.payload.body_text == "改过的正文"


async def test_a_new_letter_needs_a_plain_recipient(
    drafts: SqliteNewMailDraftRepository,
) -> None:
    with pytest.raises(InvalidNewMailDraft):
        await _new_draft(drafts, to_address="张老师 <zhang@example.edu>")
    with pytest.raises(InvalidNewMailDraft):
        await _new_draft(drafts, to_address="")


async def test_a_historical_reply_payload_still_parses() -> None:
    """A payload written before the discriminator existed keeps its meaning (ADR-0037 §7)."""
    legacy = {
        "schema_version": 1,
        "draft_id": str(uuid4()),
        "draft_version": 1,
        "account_id": SEND_ACCOUNT_ID,
        "from_address": FROM_ADDRESS,
        "to_addresses": [TO_ADDRESS],
        "subject": "Re: SE lab deadline",
        "body_text": "See you Friday.",
        "rfc_message_id": "<legacy@example.edu>",
        "date_header": "Tue, 22 Sep 2026 09:00:00 +0000",
        "in_reply_to_header": "<original@example.edu>",
        "references": ["<original@example.edu>"],
    }

    payload = MailSendPayload.from_payload(legacy)

    assert payload.kind is MailSendKind.REPLY
    assert payload.in_reply_to_header == "<original@example.edu>"
    assert payload.to_payload()["kind"] == "reply"


async def test_a_new_payload_may_not_carry_reply_headers() -> None:
    with pytest.raises(InvalidMailSend):
        MailSendPayload(
            kind=MailSendKind.NEW,
            draft_id=uuid4(),
            draft_version=1,
            account_id=SEND_ACCOUNT_ID,
            from_address=FROM_ADDRESS,
            to_addresses=(TO_ADDRESS,),
            subject=SUBJECT,
            body_text=BODY,
            rfc_message_id="<x@example.edu>",
            date_header="Tue, 22 Sep 2026 09:00:00 +0000",
            in_reply_to_header="<original@example.edu>",
        )


async def test_the_executor_adds_no_reply_headers_to_a_new_letter(
    stores: ActionStores, drafts: SqliteNewMailDraftRepository, clock: FakeClock
) -> None:
    draft = await _new_draft(drafts)
    case = await _case(stores, clock)
    preparation = await _send_service(stores, drafts, clock).prepare_new_send(draft.id, case.id)

    raw = build_rfc822_message(preparation.payload)

    assert b"In-Reply-To" not in raw
    assert b"References" not in raw
    assert preparation.payload.rfc_message_id.encode() in raw
    assert BODY.encode() in raw


# ---------------------------------------------------------------------------- status


async def test_the_status_follows_the_new_draft_version(
    stores: ActionStores, drafts: SqliteNewMailDraftRepository, clock: FakeClock
) -> None:
    draft = await _new_draft(drafts)
    case = await _case(stores, clock)
    preparation = await _send_service(stores, drafts, clock).prepare_new_send(draft.id, case.id)
    status_service = MailSendStatusService(
        stores.actions,
        SqliteMailSendRepository(stores.database),
        SqliteMailDraftRepository(stores.database),
        clock,
        new_drafts=drafts,
    )

    before = await status_service.status(preparation.action.id)

    assert before.state is MailDeliveryState.DRAFT
    assert before.current_draft_version == 1
    assert before.draft_version_changed is False
    assert before.new_draft is not None

    revised = draft.revised(at=NOW, body_text="改过的正文")
    await drafts.update_draft(revised, expected_version=draft.version)
    after = await status_service.status(preparation.action.id)

    assert after.current_draft_version == 2
    assert after.draft_version_changed is True


# ------------------------------------------------------------ execution and reconciliation


async def test_a_new_letter_executes_through_the_approval_boundary(
    stores: ActionStores, drafts: SqliteNewMailDraftRepository, clock: FakeClock
) -> None:
    draft = await _new_draft(drafts)
    case = await _case(stores, clock)
    preparation = await _send_service(stores, drafts, clock).prepare_new_send(draft.id, case.id)
    server = StrictSmtpServer()

    await _approve(stores, clock, preparation.action)
    result = await _execution(stores, clock, _smtp_executor(server)).execute(
        preparation.action.id
    )

    assert result.status is ExecutionRunStatus.SUCCEEDED
    sent = server.sent_bytes
    assert sent is not None
    assert TO_ADDRESS.encode() in sent
    assert _decoded_subject(sent) == SUBJECT
    stored = await stores.actions.get_action(preparation.action.id)
    assert stored is not None and stored.status is ActionRequestStatus.EXECUTED


async def test_a_new_letter_reconciles_by_its_stable_message_id(
    stores: ActionStores, drafts: SqliteNewMailDraftRepository, clock: FakeClock
) -> None:
    """The same Sent-folder lookup as a reply: the payload's Message-ID, nothing reconstructed."""
    draft = await _new_draft(drafts)
    case = await _case(stores, clock)
    preparation = await _send_service(stores, drafts, clock).prepare_new_send(draft.id, case.id)
    server = StrictSmtpServer(fail_at="data", error=ConnectionResetError("dropped after DATA"))
    await _approve(stores, clock, preparation.action)
    result = await _execution(stores, clock, _smtp_executor(server)).execute(
        preparation.action.id
    )
    assert result.status is ExecutionRunStatus.UNKNOWN

    lookup = FakeSentLookup().with_match(uid=7, uidvalidity=3)
    outcome = await MailSendReconciliationService(
        stores.actions,
        SqliteMailSendRepository(stores.database),
        clock,
        lookup=lookup,
        accounts=(smtp_account(),),
    ).reconcile(preparation.action.id)

    assert outcome.result.value == "found"
    assert outcome.resolved is True
    assert lookup.answered == [(SENT_MAILBOX, preparation.payload.rfc_message_id)]
    stored = await stores.actions.get_action(preparation.action.id)
    assert stored is not None and stored.status is ActionRequestStatus.EXECUTED
    refreshed = await stores.actions.latest_execution(preparation.action.id)
    assert refreshed is not None and refreshed.status is ExecutionRunStatus.SUCCEEDED


async def test_a_revision_after_approval_does_not_change_what_is_sent(
    stores: ActionStores, drafts: SqliteNewMailDraftRepository, clock: FakeClock
) -> None:
    """The action is immutable: editing the draft afterwards cannot retarget the approved letter."""
    draft = await _new_draft(drafts)
    case = await _case(stores, clock)
    preparation = await _send_service(stores, drafts, clock).prepare_new_send(draft.id, case.id)
    revised = replace(draft, body_text="一段完全不同的正文", version=2)
    await drafts.update_draft(revised, expected_version=draft.version)
    server = StrictSmtpServer()
    await _approve(stores, clock, preparation.action)

    await _execution(stores, clock, _smtp_executor(server)).execute(preparation.action.id)

    sent = server.sent_bytes
    assert sent is not None
    assert BODY.encode() in sent
    assert "一段完全不同的正文".encode() not in sent
