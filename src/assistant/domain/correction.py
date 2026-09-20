"""Correction: something the user told the assistant in their own words (ADR-0027).

A correction is the *only* provenance a candidate fact can have in V1, and that is the whole
point of the phase: personal learning starts from a person saying "this is wrong" or "this is
how it is", never from a model's summary of a message, and never from an inference about
behaviour.

It is stored as the user wrote it — trimmed, never rewritten, never summarised — because a
correction is also the audit record of why a fact exists. Removing one would break the
provenance of anything it produced, so there is no delete path in this phase.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from assistant.domain.errors import InvalidCorrection

CorrectionId = UUID
"""Stable identity of one user correction."""

CORRECTION_MAX_LENGTH = 4000
"""How much text one correction may carry."""


def new_correction_id() -> CorrectionId:
    """Generate a fresh correction identity."""
    return uuid4()


def validate_correction_text(text: str) -> str:
    """Return the trimmed correction text, or raise `InvalidCorrection`."""
    if not isinstance(text, str):
        raise InvalidCorrection("a correction must be text")
    stripped = text.strip()
    if not stripped:
        raise InvalidCorrection("a correction must not be blank")
    if len(stripped) > CORRECTION_MAX_LENGTH:
        raise InvalidCorrection(
            f"a correction must be at most {CORRECTION_MAX_LENGTH} characters"
        )
    return stripped


@dataclass(frozen=True, slots=True)
class Correction:
    """One explicit piece of user-provided feedback."""

    text: str
    created_at: datetime
    id: CorrectionId = field(default_factory=new_correction_id)

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", validate_correction_text(self.text))
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise InvalidCorrection("created_at must be timezone-aware")

    def preview(self, limit: int = 60) -> str:
        """A single-line preview for list output. The stored text is never changed."""
        collapsed = " ".join(self.text.split())
        if len(collapsed) <= limit:
            return collapsed
        return collapsed[: limit - 1] + "\u2026"


__all__ = [
    "CORRECTION_MAX_LENGTH",
    "Correction",
    "CorrectionId",
    "new_correction_id",
    "validate_correction_text",
]
