"""Content-addressed storage for normalized page text (ADR-0029).

The normalized text of an observation is written under
`<runtime>/web/snapshots/<sha-prefix>/<sha256>.txt` and addressed by its SHA-256, so the same page
content is one object no matter how many times it is seen. Writes are: write a temporary file in
the *same* directory, flush, `fsync`, then atomically rename onto the content-addressed name. An
existing object is read back and verified rather than overwritten, so a hash collision can never
silently replace different text.

Only the relative storage key is returned and only the relative storage key is persisted; no
absolute path reaches the database, an event or a log line.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from pathlib import Path

from assistant.adapters.runtime.permissions import (
    ensure_private_directory,
    ensure_private_file,
)
from assistant.domain.errors import WebSnapshotStorageError

SNAPSHOT_DIRECTORY = "web"
SNAPSHOT_SUBDIRECTORY = "snapshots"


class WebSnapshotStore:
    """Content-addressed storage for normalized watcher text."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        """The base directory snapshots live under (implementation detail, not identity)."""
        return self._root

    async def store_text(self, normalized_text: str) -> tuple[str, str]:
        """Store `normalized_text` and return its `(sha256, storage_key)`."""
        return await asyncio.to_thread(self._store_text_sync, normalized_text)

    async def read_text(self, storage_key: str) -> str:
        """Read one snapshot back, verifying its content address.

        Raises:
            WebSnapshotStorageError: the object is missing or does not match its key.
        """
        return await asyncio.to_thread(self._read_text_sync, storage_key)

    # ------------------------------------------------------------ blocking internals

    def _store_text_sync(self, normalized_text: str) -> tuple[str, str]:
        raw = normalized_text.encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        key = f"{SNAPSHOT_DIRECTORY}/{SNAPSHOT_SUBDIRECTORY}/{digest[:2]}/{digest}.txt"
        target = self._root / key
        if target.exists():
            self._verify_existing(target, raw, digest)
            return digest, key
        try:
            ensure_private_directory(target.parent)
            handle, temporary = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                ensure_private_file(Path(temporary))
                os.replace(temporary, target)
                ensure_private_file(target)
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise
        except OSError as exc:
            raise WebSnapshotStorageError(
                f"could not store the web snapshot ({type(exc).__name__})"
            ) from exc
        return digest, key

    def _read_text_sync(self, storage_key: str) -> str:
        path = self._root / storage_key
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise WebSnapshotStorageError(
                f"could not read the web snapshot ({type(exc).__name__})"
            ) from exc
        if hashlib.sha256(raw).hexdigest() != Path(storage_key).stem:
            raise WebSnapshotStorageError(
                "a stored web snapshot does not match its content address"
            )
        return raw.decode("utf-8", errors="replace")

    def _verify_existing(self, target: Path, raw: bytes, digest: str) -> None:
        """Refuse to reuse an object whose bytes disagree with its name."""
        try:
            existing = target.read_bytes()
        except OSError as exc:
            raise WebSnapshotStorageError(
                f"could not read an existing web snapshot ({type(exc).__name__})"
            ) from exc
        if hashlib.sha256(existing).hexdigest() != digest or len(existing) != len(raw):
            raise WebSnapshotStorageError(
                "an existing web snapshot does not match its content address"
            )


__all__ = ["SNAPSHOT_DIRECTORY", "SNAPSHOT_SUBDIRECTORY", "WebSnapshotStore"]
