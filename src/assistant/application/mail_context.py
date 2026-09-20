"""The bounded, untrusted context one mail analysis is allowed to see (ADR-0021).

Data minimisation is structural, not a filter applied afterwards:

- the raw `.eml`, its storage key, the mailbox cursor and the account credential are not
  reachable from here, because nothing in this module can read them;
- the recipient list, any binary part and its hash stay local: a classification needs to know
  what a message says, not who else received it;
- task, calendar, scheduler, notification and knowledge state are absent by construction, so a
  mail analysis cannot depend on the user's commitments and cannot leak them to a provider;
- the budget is explicit — per message and in total — with a deterministic truncation flag
  instead of hoping a provider window absorbs the difference.

Every description of a message is quoted data inside one JSON document. The instructions say so,
and nothing here can turn message text into an instruction.

The payload deliberately contains no reading of the clock. A retry an hour later must produce
the *same* request bytes as the first attempt; relative expressions in a mail are anchored to
the mail's own `Date`, which is also the only anchor the reader had.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from assistant.domain.mail import MailMessage, MailMessageId
from assistant.domain.mail_analysis import MailThreadId
from assistant.ports.mail_intelligence_repository import MailIntelligenceRepository
from assistant.ports.mail_repository import MailRepository

MAX_CONTEXT_CHARS_PER_MESSAGE = 3000
"""How much of one message body may reach the model."""

MAX_CONTEXT_CHARS_TOTAL = 12000
"""The whole context budget: a hard ceiling, not a target to be tested."""

MAX_THREAD_CONTEXT_MESSAGES = 5
"""How many earlier messages of the same thread may accompany the current one."""


@dataclass(frozen=True, slots=True)
class MailContextMessage:
    """One message as the model sees it."""

    message_id: MailMessageId
    subject: str | None
    from_address: str | None
    sent_at: datetime | None
    body_text: str | None
    body_truncated: bool = False

    def to_payload(self) -> dict[str, object]:
        """The canonical JSON object for this message."""
        return {
            "message_id": str(self.message_id),
            "subject": self.subject,
            "from_address": self.from_address,
            "sent_at": _instant(self.sent_at),
            "body_text": self.body_text,
            "body_truncated": self.body_truncated,
        }


@dataclass(frozen=True, slots=True)
class MailContext:
    """Everything one mail analysis needs, and nothing else."""

    current: MailContextMessage
    thread_id: MailThreadId | None = None
    """The stored thread, or `None` when the message has no membership yet.

    A draft may be written for a message the daemon has not threaded, and creating a membership
    would be a mail-state change a read-only path must not make — so `None` means "the context is
    this message alone", not "an unknown thread".
    """
    previous: tuple[MailContextMessage, ...] = ()
    planning_timezone: str | None = None
    context_truncated: bool = False
    thread_identity: tuple[tuple[MailMessageId, str], ...] = ()
    """Every message of the thread in conversation order, with its content fingerprint.

    The whole thread is recorded, not only the slice that fitted the budget: the slice is a
    deterministic function of the thread and the budget, so the thread's identity is what a
    reader needs to tell "the same question" from "a question whose context grew".
    """

    @property
    def thread_message_ids(self) -> tuple[MailMessageId, ...]:
        """Every message of the thread this context was drawn from, in order."""
        return tuple(message_id for message_id, _ in self.thread_identity)

    @property
    def thread_content_fingerprints(self) -> tuple[str, ...]:
        """The content fingerprint of every message listed by `thread_message_ids`."""
        return tuple(fingerprint for _, fingerprint in self.thread_identity)

    def to_payload(self) -> dict[str, object]:
        """The canonical JSON document placed in the user message."""
        return {
            "planning_timezone": self.planning_timezone,
            "thread_message_count": len(self.previous) + 1,
            "context_truncated": self.context_truncated,
            "thread_messages": [item.to_payload() for item in self.previous],
            "current_message": self.current.to_payload(),
        }

    def to_json(self) -> str:
        """Canonical JSON: sorted keys, compact separators, UTF-8 text."""
        return json.dumps(
            self.to_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )


class MailContextBuilder:
    """Builds the bounded context for one message. It can only read."""

    def __init__(
        self,
        mail: MailRepository,
        intelligence: MailIntelligenceRepository,
        *,
        planning_timezone: str | None = None,
        max_previous: int = MAX_THREAD_CONTEXT_MESSAGES,
        per_message_chars: int = MAX_CONTEXT_CHARS_PER_MESSAGE,
        total_chars: int = MAX_CONTEXT_CHARS_TOTAL,
    ) -> None:
        if max_previous < 0:
            raise ValueError("max_previous must not be negative")
        if per_message_chars < 1 or total_chars < per_message_chars:
            raise ValueError("the context budget must fit at least one whole message")
        self._mail = mail
        self._intelligence = intelligence
        self._planning_timezone = planning_timezone
        self._max_previous = max_previous
        self._per_message_chars = per_message_chars
        self._total_chars = total_chars

    async def build(
        self, message: MailMessage, thread_id: MailThreadId
    ) -> MailContext:
        """Return the same request bytes for the same stored state, every time."""
        thread = await self._intelligence.list_thread_messages(thread_id)
        return self.assemble(message, thread_id=thread_id, thread=thread)

    def build_standalone(self, message: MailMessage) -> MailContext:
        """Build the context of a message that has no stored thread membership.

        Used by paths that must not change mail state — a draft may be written for a message the
        daemon has not threaded yet — so the context is the message itself and nothing else. The
        thread identity is still recorded, as the single message it is.
        """
        return self.assemble(message, thread_id=None, thread=[message])

    def assemble(
        self,
        message: MailMessage,
        *,
        thread_id: MailThreadId | None,
        thread: list[MailMessage],
    ) -> MailContext:
        """Bound one ordered set of messages into the context the model may see."""
        # The thread arrives in conversation order, so "the previous messages" are the tail of
        # everything that is not the current one. Comparing timestamps instead would make the
        # history empty whenever two messages share a timestamp, which is exactly when the
        # reader most needs the surrounding conversation.
        others = [item for item in thread if item.id != message.id]
        earlier = others[-self._max_previous :] if self._max_previous else []
        current, current_cut = self._bounded(message.body_text)
        used = len(current)
        truncated = current_cut or len(earlier) < len(others)
        kept: list[MailContextMessage] = []
        for item in reversed(earlier):
            body, was_cut = self._bounded(item.body_text)
            if used + len(body) > self._total_chars:
                truncated = True
                break
            used += len(body)
            truncated = truncated or was_cut
            kept.append(_context_message(item, body=body, truncated=was_cut))
        kept.reverse()
        identity = [(item.id, item.content_fingerprint) for item in thread]
        if all(item.id != message.id for item in thread):
            # Defensive: the message is always a member by the time an analysis runs, but a
            # context must never claim the message itself is missing from its own thread.
            identity.append((message.id, message.content_fingerprint))
        return MailContext(
            thread_id=thread_id,
            current=_context_message(message, body=current, truncated=current_cut),
            previous=tuple(kept),
            planning_timezone=self._planning_timezone,
            context_truncated=truncated or len(kept) < len(earlier),
            thread_identity=tuple(identity),
        )

    def _bounded(self, body: str | None) -> tuple[str, bool]:
        """The first `per_message_chars` characters of a body, or the whole thing."""
        if body is None:
            return "", False
        if len(body) <= self._per_message_chars:
            return body, False
        return body[: self._per_message_chars], True


def _context_message(
    message: MailMessage, *, body: str, truncated: bool
) -> MailContextMessage:
    return MailContextMessage(
        message_id=message.id,
        subject=message.subject,
        from_address=message.from_address,
        sent_at=message.sent_at,
        body_text=None if message.body_text is None else body,
        body_truncated=truncated,
    )


def _instant(value: datetime | None) -> str | None:
    """Render an instant as UTC ISO 8601, or `None`."""
    return None if value is None else value.astimezone(UTC).isoformat()


__all__ = [
    "MAX_CONTEXT_CHARS_PER_MESSAGE",
    "MAX_CONTEXT_CHARS_TOTAL",
    "MAX_THREAD_CONTEXT_MESSAGES",
    "MailContext",
    "MailContextBuilder",
    "MailContextMessage",
]
