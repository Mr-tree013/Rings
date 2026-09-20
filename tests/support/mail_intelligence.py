"""Helpers for the Phase 5B tests: real stores, real bridge, scripted model (ADR-0021).

Threading and analysis are tested against real SQLite through the real repositories, because the
properties under test — one membership per message, one analysis per message, an analysis that
survives a retry — are database properties. Only the provider is scripted.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from assistant.adapters.model.fake import FakeModelAdapter
from assistant.application.event_inbox import EventInbox, IngestEvent
from assistant.application.mail_context import (
    MAX_CONTEXT_CHARS_PER_MESSAGE,
    MAX_CONTEXT_CHARS_TOTAL,
    MAX_THREAD_CONTEXT_MESSAGES,
    MailContextBuilder,
)
from assistant.application.mail_event_handler import MailInboundEventHandler
from assistant.application.mail_threading import MailThreadLinker
from assistant.application.structured_model import StructuredModel
from assistant.domain.inbound_event import InboundEvent
from assistant.domain.mail import (
    MailBodyStatus,
    MailboxSyncMode,
    MailMessage,
    MailMessageLocation,
    mail_content_fingerprint,
)
from assistant.domain.mail_analysis import ANALYZER_VERSION
from assistant.ports.mail_repository import FetchedMail
from assistant.store.db import Database
from assistant.store.events import SqliteEventRepository
from assistant.store.mail import SqliteMailRepository
from assistant.store.mail_intelligence import SqliteMailIntelligenceRepository
from tests.support.fakes import FakeClock

DEFAULT_AT = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)
"""A fixed instant, so every stored row and fingerprint is reproducible."""


class MailStores:
    """The three stores one mail-intelligence test needs, plus a running uid counter."""

    def __init__(self, database: Database, clock: FakeClock) -> None:
        self.mail = SqliteMailRepository(database)
        self.intelligence = SqliteMailIntelligenceRepository(database)
        self.events = SqliteEventRepository(database, clock)
        self.inbox = EventInbox(self.events, clock)
        self._next_uid = 1

    def next_uid(self) -> int:
        """A fresh UID for the next stored location."""
        uid = self._next_uid
        self._next_uid += 1
        return uid

    async def store(self, message: MailMessage, *, uid: int | None = None) -> MailMessage:
        """Persist one message and one location through the real batch transaction."""
        location = MailMessageLocation(
            message_id=message.id,
            account_id=message.account_id,
            mailbox_name="INBOX",
            uidvalidity=1,
            uid=self.next_uid() if uid is None else uid,
            first_seen_at=message.first_seen_at,
            last_seen_at=message.last_seen_at,
        )
        await self.mail.apply_fetched_batch(
            account_id=message.account_id,
            mailbox_name="INBOX",
            uidvalidity=1,
            last_seen_uid=location.uid,
            messages=(FetchedMail(message=message, location=location),),
            mode=MailboxSyncMode.NORMAL,
            at=message.first_seen_at,
        )
        return message

    async def bridge(self, message: MailMessage, *, at: datetime | None = None) -> InboundEvent:
        """Create the `InboundEvent` a mail sync would create, and link it.

        The payload is byte-identical to the one `MailSyncService` writes: identity only.
        """
        result = await self.inbox.ingest(
            IngestEvent(
                source=f"mail:{message.account_id}",
                event_type="mail.message.received",
                external_id=f"message:{message.id}",
                content=json.dumps(
                    {
                        "account_id": message.account_id,
                        "mail_message_id": str(message.id),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        )
        await self.mail.link_inbound_event(
            mail_message_id=message.id,
            inbound_event_id=result.event.id,
            linked_at=message.first_seen_at if at is None else at,
        )
        return result.event


def build_message(
    *,
    account_id: str = "smail",
    message_id_header: str | None = None,
    in_reply_to_header: str | None = None,
    references: tuple[str, ...] = (),
    subject: str | None = "Subject",
    from_address: str | None = "ada@example.edu",
    body_text: str | None = "body text",
    body_status: MailBodyStatus = MailBodyStatus.AVAILABLE,
    sent_at: datetime | None = DEFAULT_AT,
    at: datetime | None = DEFAULT_AT,
    message_id: UUID | None = None,
) -> MailMessage:
    """Build one stored-shape message with a content fingerprint that matches its content."""
    moment = DEFAULT_AT if at is None else at
    date_header = None if sent_at is None else sent_at.isoformat()
    if body_status is MailBodyStatus.OVERSIZE:
        body_text = None
    return MailMessage(
        id=uuid4() if message_id is None else message_id,
        account_id=account_id,
        message_id_header=message_id_header,
        in_reply_to_header=in_reply_to_header,
        references=references,
        subject=subject,
        from_address=from_address,
        body_text=body_text,
        body_status=body_status,
        date_header=date_header,
        sent_at=sent_at,
        content_fingerprint=mail_content_fingerprint(
            message_id_header=message_id_header,
            from_address=from_address,
            date_header=date_header,
            subject=subject,
            body_text=body_text,
            attachment_sha256s=(),
            size_bytes=1024,
        ),
        size_bytes=1024,
        first_seen_at=moment,
        last_seen_at=moment,
    )


def build_handler(
    database: Database,
    clock: FakeClock,
    model: FakeModelAdapter,
    *,
    planning_timezone: str | None = None,
    analyzer_version: int | None = None,
    max_previous: int = MAX_THREAD_CONTEXT_MESSAGES,
    per_message_chars: int = MAX_CONTEXT_CHARS_PER_MESSAGE,
    total_chars: int = MAX_CONTEXT_CHARS_TOTAL,
) -> MailInboundEventHandler:
    """The handler under test, over the real stores and a scripted provider."""
    mail = SqliteMailRepository(database)
    intelligence = SqliteMailIntelligenceRepository(database)
    return MailInboundEventHandler(
        mail,
        intelligence,
        MailThreadLinker(mail, intelligence, clock),
        MailContextBuilder(
            mail,
            intelligence,
            planning_timezone=planning_timezone,
            max_previous=max_previous,
            per_message_chars=per_message_chars,
            total_chars=total_chars,
        ),
        StructuredModel(model),
        clock,
        analyzer_version=ANALYZER_VERSION if analyzer_version is None else analyzer_version,
    )


def analysis_response(
    *,
    category: str = "actionable_notice",
    requires_reply: bool = False,
    summary: str = "A notice.",
    candidates: list[dict[str, object]] | None = None,
) -> str:
    """A model answer that satisfies the analysis schema."""
    return json.dumps(
        {
            "category": category,
            "requires_reply": requires_reply,
            "summary": summary,
            "action_candidates": candidates or [],
        }
    )


def candidate(
    *,
    text: str,
    temporal_kind: str = "none",
    time_text: str | None = None,
    interpreted_at: str | None = None,
) -> dict[str, object]:
    """One action candidate as the model is asked to return it."""
    return {
        "text": text,
        "temporal_kind": temporal_kind,
        "time_text": time_text,
        "interpreted_at": interpreted_at,
    }


__all__ = [
    "DEFAULT_AT",
    "MailStores",
    "analysis_response",
    "build_handler",
    "build_message",
    "candidate",
]
