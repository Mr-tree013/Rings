"""MailIntelligenceRepository port: deterministic threads and durable analyses (ADR-0021).

Two responsibilities, both mail-owned, both deliberately separate from the mail store:

- **thread membership.** The linker decides, this port records the decision and the header
  evidence behind it. A message belongs to at most one thread, enforced by the schema.
- **analysis results.** One current analysis per message, keyed by the message and stamped with
  the fingerprint of the input it was computed from — which is what lets a retried event reuse an
  analysis instead of calling a model again.

Nothing here exposes a connection, a transaction or SQL.
"""

from __future__ import annotations

from typing import Protocol

from assistant.domain.mail import MailMessage, MailMessageId
from assistant.domain.mail_analysis import (
    MailAnalysis,
    MailThread,
    MailThreadId,
    MailThreadMember,
    MailThreadSummary,
)


class MailIntelligenceRepository(Protocol):
    """Durable thread membership and mail analyses."""

    async def get_member(self, message_id: MailMessageId) -> MailThreadMember | None:
        """Return the thread membership of one message, or `None` when it has none yet."""
        ...

    async def record_member(self, member: MailThreadMember) -> MailThreadMember:
        """Store one membership decision. Recording the same message twice is a no-op."""
        ...

    async def create_thread(self, thread: MailThread) -> MailThread:
        """Store a new empty thread."""
        ...

    async def get_thread(self, thread_id: MailThreadId) -> MailThread | None:
        """Return one thread, or `None`."""
        ...

    async def resolve_thread_id(self, reference: str) -> MailThreadId:
        """Resolve a full UUID or a unique prefix to a thread id.

        Raises:
            MailThreadNotFound: nothing matches.
            AmbiguousId: several threads match.
        """
        ...

    async def list_thread_summaries(
        self, *, account_id: str | None = None, limit: int | None = 20
    ) -> list[MailThreadSummary]:
        """List threads newest first, with their message counts."""
        ...

    async def list_thread_members(
        self, thread_id: MailThreadId
    ) -> list[MailThreadMember]:
        """List one thread's members, oldest link first."""
        ...

    async def list_thread_messages(self, thread_id: MailThreadId) -> list[MailMessage]:
        """List the messages of one thread in deterministic conversation order.

        Ordering is `(sent_at is null), sent_at, first_seen_at, id`: a message without a
        parseable `Date` sorts last instead of jumping to the front, and the id breaks ties so
        the order never depends on insertion timing.
        """
        ...

    async def find_messages_by_message_id_header(
        self, *, account_id: str, message_id_header: str
    ) -> list[MailMessageId]:
        """Return every stored message in one account with that Message-ID header.

        Duplicate Message-ID values are legal, so the caller must treat more than one result as
        ambiguous rather than choosing.
        """
        ...

    async def get_analysis(self, message_id: MailMessageId) -> MailAnalysis | None:
        """Return the stored analysis of one message, or `None`."""
        ...

    async def persist_analysis(self, analysis: MailAnalysis) -> MailAnalysis:
        """Store an analysis, replacing the previous one for that message atomically."""
        ...

    async def count_analyses(self, *, account_id: str | None = None) -> int:
        """How many messages have a stored analysis."""
        ...


__all__ = ["MailIntelligenceRepository"]
