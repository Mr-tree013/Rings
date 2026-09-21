"""Durable attention storage (ADR-0042).

The port is deliberately small. A projector needs to read the live inbox and write one row; a
service needs to settle one identified row. There is no "delete", because attention is history:
a resolved row stays, and the unique index only constrains the live ones.
"""

from __future__ import annotations

from typing import Protocol

from assistant.domain.attention import (
    AttentionItem,
    AttentionItemId,
    AttentionStatus,
)


class AttentionRepository(Protocol):
    """Storage for derived user-facing attention items."""

    async def add_item(self, item: AttentionItem) -> AttentionItem:
        """Store one new attention item.

        Raises:
            AttentionStoreError: the row conflicts with an existing live identity.
        """
        ...

    async def update_item(self, item: AttentionItem) -> AttentionItem:
        """Replace one stored item by identity.

        Raises:
            AttentionItemNotFound: no such item.
        """
        ...

    async def get_item(self, item_id: AttentionItemId) -> AttentionItem | None:
        """Return one item by identity, or `None`."""
        ...

    async def find_live_by_dedupe_key(self, dedupe_key: str) -> AttentionItem | None:
        """Return the live item occupying one dedupe key, or `None`."""
        ...

    async def list_items(
        self,
        *,
        statuses: tuple[AttentionStatus, ...] | None = None,
        limit: int = 100,
    ) -> list[AttentionItem]:
        """List attention items, most urgent then oldest first, bounded by `limit`."""
        ...


__all__ = ["AttentionRepository"]
