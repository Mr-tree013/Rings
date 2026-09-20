"""The durable side of the conversation runtime (ADR-0033 §27).

The application layer depends on this protocol and never on SQLite. Every method is a single
durable fact about a thread, a message, a turn or an operation; there is no "save everything"
method, because the crash fence depends on the *order* in which those facts are written.
"""

from __future__ import annotations

from typing import Protocol

from assistant.domain.conversation import (
    ConversationMessage,
    ConversationMessageId,
    ConversationOperation,
    ConversationOperationId,
    ConversationOperationStatus,
    ConversationThread,
    ConversationThreadId,
    ConversationTurn,
    ConversationTurnId,
)


class ConversationRepository(Protocol):
    """Durable conversation history and operation outcomes."""

    # ------------------------------------------------------------------ threads

    async def add_thread(self, thread: ConversationThread) -> ConversationThread:
        """Store a new thread."""

    async def update_thread(self, thread: ConversationThread) -> ConversationThread:
        """Store the current state of an existing thread."""

    async def get_thread(self, thread_id: ConversationThreadId) -> ConversationThread | None:
        """Return one thread, or `None`."""

    async def latest_active_thread(self) -> ConversationThread | None:
        """The most recently updated ACTIVE thread, for `resume by default`."""

    async def list_threads(self, *, limit: int = 20) -> list[ConversationThread]:
        """Threads, most recently updated first."""

    # ------------------------------------------------------------------ messages

    async def add_message(self, message: ConversationMessage) -> ConversationMessage:
        """Store one message."""

    async def get_message(self, message_id: ConversationMessageId) -> ConversationMessage | None:
        """Return one message, or `None`."""

    async def list_messages(
        self, thread_id: ConversationThreadId, *, limit: int | None = None
    ) -> list[ConversationMessage]:
        """Messages of one thread, oldest first, optionally capped to the newest `limit`."""

    # ------------------------------------------------------------------ turns

    async def add_turn(self, turn: ConversationTurn) -> ConversationTurn:
        """Store a new turn."""

    async def update_turn(self, turn: ConversationTurn) -> ConversationTurn:
        """Store the current state of an existing turn."""

    async def get_turn(self, turn_id: ConversationTurnId) -> ConversationTurn | None:
        """Return one turn, or `None`."""

    async def list_turns(
        self, thread_id: ConversationThreadId, *, limit: int | None = None
    ) -> list[ConversationTurn]:
        """Turns of one thread, oldest first."""

    async def turns_waiting_for_confirmation(
        self, thread_id: ConversationThreadId
    ) -> list[ConversationTurn]:
        """Turns with a pending confirmation, oldest first."""

    # ------------------------------------------------------------------ operations

    async def add_operation(self, operation: ConversationOperation) -> ConversationOperation:
        """Store a new operation."""

    async def update_operation(self, operation: ConversationOperation) -> ConversationOperation:
        """Store the current state of an existing operation."""

    async def get_operation(
        self, operation_id: ConversationOperationId
    ) -> ConversationOperation | None:
        """Return one operation, or `None`."""

    async def list_operations(self, turn_id: ConversationTurnId) -> list[ConversationOperation]:
        """Operations of one turn, in plan order."""

    async def operations_waiting_for_confirmation(
        self, thread_id: ConversationThreadId
    ) -> list[ConversationOperation]:
        """Operations of a thread that are waiting for the user's yes, oldest first."""

    async def list_operations_by_status(
        self, status: ConversationOperationStatus, *, limit: int = 100
    ) -> list[ConversationOperation]:
        """Operations in one status, oldest first — the crash-fence scan uses this."""
