"""Durable conversational reviews of external actions (ADR-0034 §4).

The application layer depends on this protocol and never on SQLite. A review is written once when
the action is prepared and updated only when the human settles it, cancels it, or it goes stale —
four small facts, each of which has to survive a restart.
"""

from __future__ import annotations

from typing import Protocol

from assistant.domain.conversation_review import (
    ConversationExternalReview,
    ConversationExternalReviewId,
    ConversationExternalReviewStatus,
)


class ConversationReviewRepository(Protocol):
    """Durable reviews of external actions."""

    async def add_review(self, review: ConversationExternalReview) -> ConversationExternalReview:
        """Store a new review.

        Raises:
            StoreError: the database refuses it, e.g. a second live review for one thread.
        """

    async def update_review(
        self, review: ConversationExternalReview
    ) -> ConversationExternalReview:
        """Store the current state of an existing review."""

    async def get_review(
        self, review_id: ConversationExternalReviewId
    ) -> ConversationExternalReview | None:
        """Return one review, or `None`."""

    async def get_review_for_operation(
        self, operation_id: object
    ) -> ConversationExternalReview | None:
        """Return the review a conversation operation prepared, if any."""

    async def waiting_for_thread(
        self, thread_id: object
    ) -> list[ConversationExternalReview]:
        """Live reviews of one conversation thread, oldest first."""

    async def list_by_status(
        self, status: ConversationExternalReviewStatus, *, limit: int = 100
    ) -> list[ConversationExternalReview]:
        """Reviews in one state, oldest first — the integrity and recovery scans use this."""

    async def list_reviews(self, *, limit: int = 200) -> list[ConversationExternalReview]:
        """Recent reviews, newest first."""
