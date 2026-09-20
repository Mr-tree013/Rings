"""The explicit list of what a conversation may do (ADR-0033 §6-7, §30).

This is the single place where the question "may a conversation do this?" is answered. It is data:
a set of `ConversationCapability` values, each naming an operation type, a deterministic
`ConfirmationPolicy` and the handler that performs it.

Two properties make it a boundary rather than a convenience:

- **The model never chooses a policy.** Risk classification lives here, in code the model cannot
  write to and cannot see (the schema does not mention it).
- **Only the operations below exist.** A model answer naming anything else is refused by the
  registry even if it somehow got past the schema, and an operation naming a policy that does not
  exist (`EXTERNAL_WRITE`) cannot be constructed at all.

Phase 10A ships no external-write capability and no `case.*`, `action.*`, `approval.*`,
`execution.*`, `mail.send`, `ehall.*`, `fact.*` or `playbook.*` operation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from assistant.domain.conversation_plan import (
    ConversationOperationArguments,
    ConversationOperationType,
)
from assistant.domain.errors import ConversationCapabilityUnavailable


class ConfirmationPolicy(StrEnum):
    """How much consent one operation needs before the runtime may perform it."""

    READ = "read"
    """Reads only. Executes immediately (ADR-0033 §10)."""

    LOCAL_WRITE = "local_write"
    """A local write whose target and arguments are unambiguous. Executes immediately (§11)."""

    CONFIRM_LOCAL = "confirm_local"
    """A bulk or structurally significant local write. Needs the user's explicit yes (§13)."""


@dataclass(frozen=True, slots=True)
class OperationResult:
    """What one executed operation produced, as data the renderer turns into words."""

    kind: str
    ref: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


OperationHandler = Callable[[ConversationOperationArguments], Awaitable[OperationResult]]
"""A handler takes typed arguments and returns a result. It is never given raw model output."""


@dataclass(frozen=True, slots=True)
class PreflightContext:
    """What the rest of the plan says, so a dependency inside one turn is not a refusal.

    A legal turn may draft a reply and prepare its send in the same message, or create a calendar
    event and then plan the week around it. The readiness check for the second operation must know
    that the first one is coming (ADR-0035 §15-§16).
    """

    preceding: tuple[ConversationOperationType, ...] = ()


PreflightCheck = Callable[
    [ConversationOperationArguments, PreflightContext], Awaitable[str | None]
]
"""A read-only readiness check: `None` means "ready", a string is the reason it is not."""


@dataclass(frozen=True, slots=True)
class ConversationCapability:
    """One operation a conversation may propose."""

    operation_type: ConversationOperationType
    policy: ConfirmationPolicy
    handler: OperationHandler
    description: str = ""
    preflight: PreflightCheck | None = None
    """Run for every operation in a turn *before* the first mutation (ADR-0035 §15-§16)."""


class ConversationCapabilityRegistry:
    """A closed, code-owned mapping from operation type to capability."""

    def __init__(self, capabilities: Iterable[ConversationCapability]) -> None:
        by_type: dict[ConversationOperationType, ConversationCapability] = {}
        for capability in capabilities:
            if capability.operation_type in by_type:
                raise ValueError(f"duplicate capability: {capability.operation_type.value}")
            by_type[capability.operation_type] = capability
        if not by_type:
            raise ValueError("a conversation capability registry cannot be empty")
        self._by_type = by_type

    @property
    def operation_types(self) -> tuple[ConversationOperationType, ...]:
        """Every operation this build offers, in a stable order."""
        return tuple(sorted(self._by_type, key=lambda item: item.value))

    def get(
        self, operation_type: ConversationOperationType
    ) -> ConversationCapability | None:
        """Return the capability, or `None` when it does not exist."""
        return self._by_type.get(operation_type)

    def require(self, operation_type: ConversationOperationType) -> ConversationCapability:
        """Return the capability, or refuse the turn.

        Raises:
            ConversationCapabilityUnavailable: this build does not offer the operation.
        """
        capability = self._by_type.get(operation_type)
        if capability is None:
            raise ConversationCapabilityUnavailable(
                f"this version of Tree does not offer {operation_type.value}"
            )
        return capability

    def policy_of(self, operation_type: ConversationOperationType) -> ConfirmationPolicy:
        """The code-owned confirmation policy for one operation."""
        return self.require(operation_type).policy


__all__ = [
    "ConfirmationPolicy",
    "ConversationCapability",
    "ConversationCapabilityRegistry",
    "OperationHandler",
    "OperationResult",
    "PreflightCheck",
    "PreflightContext",
]
