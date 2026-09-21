"""The bounded context one conversation turn is allowed to see (ADR-0033 §10-11).

Data minimisation is the design, exactly as in the interpreter context (ADR-0018): the model sees
the *shape* of the user's day, never its contents. Included: the current time, the configured
planning timezone, the last few messages, and a bounded list of recent entities the user may refer
to ("刚才那个任务"). Excluded by construction — not filtered later, simply not read here: mail
bodies, knowledge documents, Action payloads, Approval data, credentials, ConfirmedFacts and
Playbooks.

The entity list is what makes follow-ups safe: the model may only reference an identity that is in
this list, and the runtime re-resolves that identity through the existing service rules before
anything is touched.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from assistant.domain.conversation import ConversationMessageRole

MAX_CONTEXT_MESSAGES = 20
"""How many recent messages travel with one turn, newest last."""

MAX_CONTEXT_HISTORY_CHARS = 16000
"""Total characters of conversation history attached to a turn, newest first."""

MAX_CONTEXT_ENTITIES_PER_KIND = 12
"""How many tasks, proposals, events and notifications the model may see, per kind."""


class ConversationEntityKind(StrEnum):
    """The kinds of local entity a follow-up may refer to."""

    TASK = "task"
    PROPOSAL = "proposal"
    CALENDAR_EVENT = "calendar_event"
    RECURRING_CALENDAR_RULE = "recurring_calendar_rule"
    NOTIFICATION = "notification"
    MAIL_MESSAGE = "mail_message"
    MAIL_THREAD = "mail_thread"
    MAIL_DRAFT = "mail_draft"


@dataclass(frozen=True, slots=True)
class ConversationRecentMessage:
    """One message of bounded history."""

    role: ConversationMessageRole
    text: str
    created_at: datetime

    def to_payload(self) -> dict[str, object]:
        """The JSON representation sent to the provider."""
        return {
            "role": self.role.value,
            "text": self.text,
            "at": _instant(self.created_at),
        }


@dataclass(frozen=True, slots=True)
class ConversationEntityRef:
    """One identity the model is authorised to reference in this turn."""

    kind: ConversationEntityKind
    id: str
    label: str
    detail: str | None = None

    def to_payload(self) -> dict[str, object]:
        """The JSON representation sent to the provider."""
        return {"kind": self.kind.value, "id": self.id, "label": self.label, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class ConversationContext:
    """Everything the model is told about the user's current state, and nothing more."""

    current_time: datetime
    planning_timezone: str | None
    recent_messages: tuple[ConversationRecentMessage, ...]
    entities: tuple[ConversationEntityRef, ...]
    history_truncated: bool
    confirmation_pending: bool
    capabilities: dict[str, object] | None = None
    """What this build can do, computed from the runtime (ADR-0035 §19). Never credentials."""

    @property
    def message_ids(self) -> frozenset[str]:
        """Identities the model may reference: exactly what is in this context."""
        return frozenset(entity.id for entity in self.entities)

    def to_payload(self) -> dict[str, object]:
        """The canonical JSON document placed in the user message."""
        return {
            "current_time": _instant(self.current_time),
            "planning_timezone": self.planning_timezone,
            "conversation": {
                "recent_messages": [message.to_payload() for message in self.recent_messages],
                "history_truncated": self.history_truncated,
                "confirmation_pending": self.confirmation_pending,
            },
            "recent_entities": [entity.to_payload() for entity in self.entities],
            "capabilities": self.capabilities,
        }

    def to_json(self) -> str:
        """Canonical JSON: sorted keys, compact separators, UTF-8 text."""
        return json.dumps(
            self.to_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )


def _instant(value: datetime) -> str:
    """Render an instant as UTC ISO 8601."""
    return value.astimezone(UTC).isoformat()


__all__ = [
    "MAX_CONTEXT_ENTITIES_PER_KIND",
    "MAX_CONTEXT_HISTORY_CHARS",
    "MAX_CONTEXT_MESSAGES",
    "ConversationContext",
    "ConversationEntityKind",
    "ConversationEntityRef",
    "ConversationRecentMessage",
]
