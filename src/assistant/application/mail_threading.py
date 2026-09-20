"""Deterministic mail threading: code decides message identity (ADR-0021).

```text
ensure_thread(M)
    │ already decided? ──► return the stored decision
    ▼
candidates: In-Reply-To, then References newest → oldest
    │
    ├── exactly one stored message in this account ──► LINKED to it
    ├── more than one ────────────────────────────────► AMBIGUOUS, own thread
    └── none ─────────────────────────────────────────► UNRESOLVED (or ROOT), own thread
```

The model is not involved and cannot be: a thread is a fact about headers, and asking a
language model which message a reply belongs to would make the answer depend on a prompt.

Three rules protect the result:

- **ambiguity is never resolved by choosing.** Duplicate `Message-ID` values are legal in the
  real world, so a header that matches two stored messages produces `AMBIGUOUS` and a thread of
  its own rather than a coin flip;
- **linking is per account.** A message never joins another account's thread, even when the
  `Message-ID` matches — two mailboxes can legitimately contain the same header;
- **the walk is bounded and cycle-safe.** Recursion carries a visited set and a depth limit, so
  a message that (incorrectly) references one of its own descendants still terminates.

Every decision is recorded once. `ensure_thread` is idempotent: the second call for a message
returns the first decision instead of re-deciding it, which is what makes a later-arriving
parent a *new* root rather than a rewrite of history.
"""

from __future__ import annotations

from datetime import datetime

from assistant.domain.errors import MailMessageNotFound, MailThreadNotFound
from assistant.domain.mail import MailMessage, MailMessageId, normalize_message_id
from assistant.domain.mail_analysis import (
    MailLinkStatus,
    MailThread,
    MailThreadMember,
    validate_thread_account,
)
from assistant.ports.clock import Clock
from assistant.ports.mail_intelligence_repository import MailIntelligenceRepository
from assistant.ports.mail_repository import MailRepository

MAX_REFERENCE_CANDIDATES = 16
"""How many reference headers one message may offer as parent evidence."""

MAX_LINK_DEPTH = 32
"""How deep a parent chain may be followed before the walk refuses to go further."""

MAX_LINK_EVIDENCE_CHARS = 200
"""How much of the matching header is kept as the recorded evidence."""


class MailThreadLinker:
    """Assigns each stored message to exactly one thread, deterministically."""

    def __init__(
        self,
        mail: MailRepository,
        intelligence: MailIntelligenceRepository,
        clock: Clock,
        *,
        max_depth: int = MAX_LINK_DEPTH,
    ) -> None:
        if max_depth < 1:
            raise ValueError("max_depth must be at least 1")
        self._mail = mail
        self._intelligence = intelligence
        self._clock = clock
        self._max_depth = max_depth

    async def ensure_thread(self, message_id: MailMessageId) -> MailThreadMember:
        """Return `message_id`'s thread membership, deciding it first if it is new.

        Raises:
            MailMessageNotFound: the message is not stored.
            InvalidMailMessage: a link would cross accounts.
        """
        return await self._ensure(message_id, frozenset(), 0)

    async def _ensure(
        self,
        message_id: MailMessageId,
        visiting: frozenset[MailMessageId],
        depth: int,
    ) -> MailThreadMember:
        stored = await self._intelligence.get_member(message_id)
        if stored is not None:
            return stored
        message = await self._mail.get_message(message_id)
        if message is None:
            raise MailMessageNotFound(message_id)
        now = self._clock.now()
        if depth >= self._max_depth:
            # Not a guess: the link is simply not decidable within the configured bound, so the
            # message starts a thread of its own and the reason is recorded.
            parent: MailMessageId | None = None
            status = MailLinkStatus.ROOT
            evidence: str | None = f"depth limit {self._max_depth} reached"
        else:
            parent, status, evidence = await self._resolve_parent(message, visiting)
        if parent is None:
            return await self._intelligence.record_member(
                await self._start_thread(message, status=status, evidence=evidence, at=now)
            )
        parent_member = await self._ensure(parent, visiting | {message_id}, depth + 1)
        thread = await self._intelligence.get_thread(parent_member.thread_id)
        if thread is None:  # pragma: no cover - the member's foreign key guarantees the thread
            raise MailThreadNotFound(parent_member.thread_id)
        validate_thread_account(message.account_id, thread.account_id)
        return await self._intelligence.record_member(
            MailThreadMember(
                message_id=message.id,
                thread_id=thread.id,
                parent_message_id=parent,
                link_status=MailLinkStatus.LINKED,
                link_evidence=evidence,
                linked_at=now,
            )
        )

    async def _start_thread(
        self,
        message: MailMessage,
        *,
        status: MailLinkStatus,
        evidence: str | None,
        at: datetime,
    ) -> MailThreadMember:
        thread = await self._intelligence.create_thread(
            MailThread(account_id=message.account_id, created_at=at, updated_at=at)
        )
        return MailThreadMember(
            message_id=message.id,
            thread_id=thread.id,
            link_status=status,
            link_evidence=evidence,
            linked_at=at,
        )

    async def _resolve_parent(
        self, message: MailMessage, visiting: frozenset[MailMessageId]
    ) -> tuple[MailMessageId | None, MailLinkStatus, str | None]:
        """The first decidable parent among this message's reference headers."""
        candidates = reference_candidates(message)
        if not candidates:
            return None, MailLinkStatus.ROOT, None
        for header in candidates:
            matches = await self._intelligence.find_messages_by_message_id_header(
                account_id=message.account_id, message_id_header=header
            )
            # A message is never its own parent, even when it repeats a header it references.
            matches = [match for match in matches if match != message.id]
            if len(matches) > 1:
                # Two stored messages claim this header. Choosing one would silently attach the
                # message to the wrong conversation, so it gets its own thread instead.
                return None, MailLinkStatus.AMBIGUOUS, _evidence(header)
            if len(matches) == 1:
                parent = matches[0]
                if parent in visiting:
                    return None, MailLinkStatus.ROOT, f"cycle through {_evidence(header)}"
                return parent, MailLinkStatus.LINKED, _evidence(header)
        return None, MailLinkStatus.UNRESOLVED, _evidence(candidates[0])


def reference_candidates(message: MailMessage) -> tuple[str, ...]:
    """The headers that may name this message's parent, in resolution order.

    `In-Reply-To` is the immediate parent and comes first. `References` is an ancestor chain
    written oldest-first, so it is read back-to-front: the nearest ancestor is the best
    available parent when the immediate one was never stored.
    """
    ordered: list[str] = []
    reply_to = normalize_message_id(message.in_reply_to_header)
    if reply_to is not None:
        ordered.append(reply_to)
    for raw in reversed(message.references):
        normalized = normalize_message_id(raw)
        if normalized is not None and normalized not in ordered:
            ordered.append(normalized)
    return tuple(ordered[:MAX_REFERENCE_CANDIDATES])


def _evidence(header: str) -> str:
    if len(header) <= MAX_LINK_EVIDENCE_CHARS:
        return header
    return header[: MAX_LINK_EVIDENCE_CHARS - 1] + "\u2026"


__all__ = [
    "MAX_LINK_DEPTH",
    "MAX_LINK_EVIDENCE_CHARS",
    "MAX_REFERENCE_CANDIDATES",
    "MailThreadLinker",
    "reference_candidates",
]
