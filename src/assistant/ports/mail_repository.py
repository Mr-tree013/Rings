"""MailRepository port: durable mail state and the batch that keeps it consistent (ADR-0020).

Two properties live behind this port, and both are reasons it is not generic CRUD:

- **one fetch batch is one transaction.** Messages, locations, attachments and the cursor
  advance together, or not at all; a half-applied batch would leave a cursor that has already
  skipped mail the database never stored;
- **reconciliation needs evidence lookup.** After a mailbox rebuild, the caller has to ask
  "have I already stored a message that looks like this?" before it may reuse a stable id.

Nothing here exposes a connection, a transaction or SQL.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from assistant.domain.inbound_event import EventId
from assistant.domain.mail import (
    MailAccountId,
    MailAttachmentMetadata,
    MailboxSyncMode,
    MailboxSyncState,
    MailMessage,
    MailMessageId,
    MailMessageLocation,
    ReconciliationCandidates,
)


@dataclass(frozen=True, slots=True)
class FetchedMail:
    """One processed message, ready to be stored as a unit."""

    message: MailMessage
    location: MailMessageLocation
    attachments: tuple[MailAttachmentMetadata, ...] = ()


@dataclass(frozen=True, slots=True)
class BatchApplyResult:
    """How a batch landed: new logical messages, matched ones, and known locations."""

    created_messages: int = 0
    matched_messages: int = 0
    existing_locations: int = 0


class MailRepository(Protocol):
    """Durable mail messages, their locations and the per-mailbox cursor."""

    async def get_sync_state(
        self, account_id: MailAccountId, mailbox_name: str
    ) -> MailboxSyncState | None:
        """Return the stored cursor for one mailbox, or `None` when it was never synced."""
        ...

    async def apply_fetched_batch(
        self,
        *,
        account_id: MailAccountId,
        mailbox_name: str,
        uidvalidity: int,
        last_seen_uid: int,
        messages: Sequence[FetchedMail],
        mode: MailboxSyncMode,
        at: datetime,
        reconciled: bool = False,
    ) -> BatchApplyResult:
        """Store one fetch batch and advance the cursor in a single transaction.

        Messages whose location is already recorded are counted as existing instead of being
        stored twice, so re-processing a batch is idempotent. Any failure rolls the whole batch
        back and leaves the cursor untouched.
        """
        ...

    async def get_message(self, message_id: MailMessageId) -> MailMessage | None:
        """Return one stored message, or `None`."""
        ...

    async def list_messages(
        self, *, account_id: MailAccountId | None = None, limit: int | None = 20
    ) -> list[MailMessage]:
        """List stored messages, newest first (`sent_at` first, then first-seen)."""
        ...

    async def list_locations(self, message_id: MailMessageId) -> list[MailMessageLocation]:
        """List the places one message was seen, oldest first."""
        ...

    async def list_attachments(
        self, message_id: MailMessageId
    ) -> list[MailAttachmentMetadata]:
        """List one message's attachment metadata in `ordinal` order."""
        ...

    async def list_unlinked_messages(self, *, limit: int = 100) -> list[MailMessage]:
        """List stored messages that have no `InboundEvent` link yet, oldest first."""
        ...

    async def link_inbound_event(
        self,
        *,
        mail_message_id: MailMessageId,
        inbound_event_id: EventId,
        linked_at: datetime,
    ) -> None:
        """Record that one message is bridged to one event. Repeating it is a no-op."""
        ...

    async def get_linked_event_id(self, message_id: MailMessageId) -> EventId | None:
        """Return the `InboundEvent` this message is bridged to, or `None` when it is not."""
        ...

    async def count_messages(self, *, account_id: MailAccountId | None = None) -> int:
        """How many logical messages are stored."""
        ...

    async def count_unlinked_messages(
        self, *, account_id: MailAccountId | None = None
    ) -> int:
        """How many stored messages still lack an event link."""
        ...

    async def find_reconciliation_candidates(
        self,
        *,
        account_id: MailAccountId,
        message_id_header: str | None,
        content_fingerprint: str,
        raw_sha256: str | None,
    ) -> ReconciliationCandidates:
        """Find stored messages that might be the same logical message as this evidence."""
        ...

    async def resolve_message_id(self, reference: str) -> MailMessageId:
        """Resolve a full UUID or a unique prefix to a message id.

        Raises:
            MailMessageNotFound: nothing matches.
            AmbiguousId: several messages match; the caller never guesses.
        """
        ...


__all__ = ["BatchApplyResult", "FetchedMail", "MailRepository"]
