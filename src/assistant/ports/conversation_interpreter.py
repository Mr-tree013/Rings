"""Turning one user message into one typed conversation plan (ADR-0033 §6).

This is the conversation counterpart of the ADR-0018 interpreter, and it keeps the same property:
its output is a *plan*, so nothing it returns has happened yet. The runtime decides what is
allowed; the interpreter only ever proposes.
"""

from __future__ import annotations

from typing import Protocol

from assistant.domain.conversation_context import ConversationContext
from assistant.domain.conversation_plan import ConversationPlan


class ConversationInterpreter(Protocol):
    """A typed interpreter over a bounded conversation context."""

    version: str
    """The prompt-and-schema version recorded on every turn."""

    async def plan(self, text: str, context: ConversationContext) -> ConversationPlan:
        """Turn one user message into one validated plan."""
