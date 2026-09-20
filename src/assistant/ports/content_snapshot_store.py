"""ContentSnapshotStore port: read a normalized snapshot back by its content address (ADR-0029).

Writing is the fetching adapter's business — it is the only place that has the bytes and the
normalized text in hand, and it stores them content-addressed before the observation row exists.
Reading is a small port because one other thing needs the text: building the bounded change context
a model is allowed to see.

The key is always the *relative* storage key recorded on the observation. No absolute path crosses
this boundary, and nothing outside the adapter chooses where snapshots live.
"""

from __future__ import annotations

from typing import Protocol


class ContentSnapshotStore(Protocol):
    """Reads normalized text snapshots by content address."""

    async def read_text(self, storage_key: str) -> str:
        """Return the normalized text stored at `storage_key`.

        Raises:
            WebSnapshotStorageError: the object is missing or does not match its address.
        """
        ...


__all__ = ["ContentSnapshotStore"]
