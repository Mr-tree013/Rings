"""ContentObjectReader port: read one referenced content object by its storage key (ADR-0031).

The integrity checker has to read raw mail and web snapshots to compare them with the hashes the
database recorded, and it must do so without knowing where either store keeps its files. This port
is that seam: the adapter resolves a storage key to the right store, and the application layer only
ever sees bytes.

Reading is the whole interface. Nothing writes, and nothing here can enumerate a directory: the
checker looks at the objects the database references and at nothing else.
"""

from __future__ import annotations

from typing import Protocol


class ContentObjectReader(Protocol):
    """Reads one content object by its database storage key."""

    async def read_object(self, storage_key: str) -> bytes:
        """Return the bytes of `storage_key`.

        Raises:
            DomainError: the object is missing or does not match its content address, as the
                relevant store defines it.
        """
        ...


__all__ = ["ContentObjectReader"]
