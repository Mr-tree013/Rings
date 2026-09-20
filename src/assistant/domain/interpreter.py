"""What an interpretation produced: a draft, a question, or an honest refusal (ADR-0018).

There is no `confidence`, no `rationale` and no `analysis` field. A model's self-reported
confidence is not a business fact, and a rationale is not something this project stores, shows
or acts on.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from assistant.domain.command import CommandDraft
from assistant.domain.errors import InvalidInterpretationResult


class InterpretationStatus(StrEnum):
    """The three outcomes an interpretation may have."""

    READY = "ready"
    NEEDS_CLARIFICATION = "needs_clarification"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class InterpretationResult:
    """One interpretation: exactly one of a draft, a question, or a reason."""

    status: InterpretationStatus
    command: CommandDraft | None = None
    question: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status is InterpretationStatus.READY:
            if self.command is None:
                raise InvalidInterpretationResult("a READY result needs a command draft")
            if self.question is not None or self.reason is not None:
                raise InvalidInterpretationResult(
                    "a READY result carries neither a question nor a reason"
                )
        elif self.status is InterpretationStatus.NEEDS_CLARIFICATION:
            if self.command is not None or self.reason is not None:
                raise InvalidInterpretationResult(
                    "a NEEDS_CLARIFICATION result carries only a question"
                )
            if self.question is None or not self.question.strip():
                raise InvalidInterpretationResult(
                    "a NEEDS_CLARIFICATION result needs a non-blank question"
                )
        else:
            if self.command is not None or self.question is not None:
                raise InvalidInterpretationResult(
                    "an UNSUPPORTED result carries only a reason"
                )
            if self.reason is None or not self.reason.strip():
                raise InvalidInterpretationResult(
                    "an UNSUPPORTED result needs a non-blank reason"
                )

    @classmethod
    def ready(cls, command: CommandDraft) -> InterpretationResult:
        """A validated draft, ready for the user to review."""
        return cls(status=InterpretationStatus.READY, command=command)

    @classmethod
    def needs_clarification(cls, question: str) -> InterpretationResult:
        """One concise question, because the request cannot be interpreted as written."""
        return cls(
            status=InterpretationStatus.NEEDS_CLARIFICATION, question=question.strip()
        )

    @classmethod
    def unsupported(cls, reason: str) -> InterpretationResult:
        """An honest refusal: this capability does not exist yet."""
        return cls(status=InterpretationStatus.UNSUPPORTED, reason=reason.strip())


__all__ = ["InterpretationResult", "InterpretationStatus"]
