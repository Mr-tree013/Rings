"""Manual input: text a person pasted in on purpose (ADR-0029).

`pw ingest text "..."` exists because a great deal of what a student needs to act on arrives
somewhere a watcher cannot see: a forwarded QQ message, a note read aloud, a screenshot they typed
out. The project stores it, fingerprints it and queues it — and treats it as **untrusted quoted
data**, because forwarded text is somebody else's words and may contain anything at all, including
instructions aimed at whatever reads it next.

Two consequences, both deliberate:

- the text is durable *before* it is bridged to `EventInbox`, so a crash between the two is
  repairable rather than lossy;
- nothing here can choose a channel with executable semantics. `source` is a small closed set of
  names, and the bridge publishes only the input id and that name — never the text.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from assistant.domain.errors import InvalidManualInput

ManualInputId = UUID
"""Stable identity of one manual input."""

MANUAL_INPUT_MAX_CHARS = 20_000
"""How much text one manual input may carry."""


def new_manual_input_id() -> ManualInputId:
    """Generate a fresh manual-input identity."""
    return uuid4()


class ManualInputSource(StrEnum):
    """Where a manual input came from. A name, never a capability."""

    MANUAL = "manual"
    QQ_FORWARD = "qq-forward"
    OTHER = "other"


def validate_manual_input_text(text: str) -> str:
    """Return the trimmed text, or raise `InvalidManualInput`."""
    if not isinstance(text, str):
        raise InvalidManualInput("manual input must be text")
    stripped = text.strip()
    if not stripped:
        raise InvalidManualInput("manual input must not be blank")
    if len(stripped) > MANUAL_INPUT_MAX_CHARS:
        raise InvalidManualInput(
            f"manual input must be at most {MANUAL_INPUT_MAX_CHARS} characters"
        )
    return stripped


def manual_input_sha256(text: str) -> str:
    """The content identity of one manual input: SHA-256 of its UTF-8 text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ManualInput:
    """One piece of text a person handed to the assistant."""

    text: str
    source: ManualInputSource
    created_at: datetime
    id: ManualInputId = field(default_factory=new_manual_input_id)
    content_sha256: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", validate_manual_input_text(self.text))
        if not isinstance(self.source, ManualInputSource):
            try:
                object.__setattr__(self, "source", ManualInputSource(self.source))
            except ValueError as exc:
                allowed = ", ".join(item.value for item in ManualInputSource)
                raise InvalidManualInput(
                    f"unknown manual input source {self.source!r}; expected one of: {allowed}"
                ) from exc
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise InvalidManualInput("created_at must be timezone-aware")
        digest = self.content_sha256 or manual_input_sha256(self.text)
        if digest != manual_input_sha256(self.text):
            raise InvalidManualInput("the content hash does not match the stored text")
        object.__setattr__(self, "content_sha256", digest)

    def preview(self, limit: int = 60) -> str:
        """A single-line preview for list output. The stored text is never changed."""
        collapsed = " ".join(self.text.split())
        if len(collapsed) <= limit:
            return collapsed
        return collapsed[: limit - 1] + "\u2026"


__all__ = [
    "MANUAL_INPUT_MAX_CHARS",
    "ManualInput",
    "ManualInputId",
    "ManualInputSource",
    "manual_input_sha256",
    "new_manual_input_id",
    "validate_manual_input_text",
]
