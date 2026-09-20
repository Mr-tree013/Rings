"""Reading referenced content objects out of the runtime directory (ADR-0031).

The two stores verify their own content addresses when they read: the raw mail store checks that the
file's SHA-256 matches its name, and the snapshot store does the same for normalized page text. This
adapter simply routes a storage key to the right one, so `IntegrityService` can compare what it gets
with the hash the *database* recorded — which is a different question from "is this file intact",
and the one that catches a swapped or stale object.
"""

from __future__ import annotations

from pathlib import Path

from assistant.adapters.mail.raw_store import RawMailStore
from assistant.adapters.web_watch.snapshot_store import WebSnapshotStore
from assistant.domain.backup import MAIL_MEMBER_PREFIX, MAIL_ROOT_DIRECTORY
from assistant.domain.errors import InvalidBackupArchive


class RuntimeContentObjects:
    """Reads one content object from whichever store owns its key."""

    def __init__(self, runtime: Path) -> None:
        self._runtime = Path(runtime)
        self._mail = RawMailStore(self._runtime / MAIL_ROOT_DIRECTORY)
        self._web = WebSnapshotStore(self._runtime)

    async def read_object(self, storage_key: str) -> bytes:
        """Return the bytes of one referenced object.

        Raises:
            WebSnapshotStorageError, MailRawStorageError: the object is missing or does not match
                its own content address.
            InvalidBackupArchive: the key does not belong to either store.
        """
        if storage_key.startswith(MAIL_MEMBER_PREFIX):
            return await self._mail.read(storage_key)
        if storage_key.startswith("web/snapshots/"):
            text = await self._web.read_text(storage_key)
            return text.encode("utf-8")
        raise InvalidBackupArchive(f"{storage_key!r} is not a content object this host stores")


__all__ = ["RuntimeContentObjects"]
