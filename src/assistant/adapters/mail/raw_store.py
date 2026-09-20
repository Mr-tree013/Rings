"""Content-addressed raw message storage (ADR-0020).

Raw RFC822 bytes are the only copy of a message this project keeps: the parsed columns are a
reading of them, and attachments stay inside them. They live under the runtime data directory
(`~/.local/share/growing-assistant/mail/raw/<prefix>/<sha256>.eml`), never in the repository,
never in a vault, and never in the cache — a cache may be deleted at any time.

Writes are: hash the bytes, write a temporary file in the *same* directory, flush and `fsync`,
then atomically rename onto the content-addressed name. An existing object is verified and
reused rather than overwritten, so a second write of the same content is a no-op and a hash
collision can never silently replace different bytes.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from assistant.adapters.runtime.permissions import (
    ensure_private_directory,
    ensure_private_file,
)
from assistant.domain.errors import MailRawStorageError

RAW_DIRECTORY = "mail"
RAW_SUBDIRECTORY = "raw"


@dataclass(frozen=True, slots=True)
class RawMailObject:
    """A stored raw message: its hash and the *relative* key that addresses it."""

    sha256: str
    storage_key: str
    size_bytes: int


class RawMailStore:
    """Content-addressed storage for raw `.eml` objects."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        """The base directory raw objects live under (implementation detail, not identity)."""
        return self._root

    async def store(self, raw: bytes) -> RawMailObject:
        """Persist `raw` and return its content-addressed object.

        Raises:
            MailRawStorageError: the bytes could not be stored or verified.
        """
        return await asyncio.to_thread(self._store_sync, raw)

    async def read(self, storage_key: str) -> bytes:
        """Read one stored object back, verifying its hash.

        Raises:
            MailRawStorageError: the object is missing or does not match its key.
        """
        return await asyncio.to_thread(self._read_sync, storage_key)

    # ------------------------------------------------------------ blocking internals

    def _store_sync(self, raw: bytes) -> RawMailObject:
        digest = hashlib.sha256(raw).hexdigest()
        key = f"{RAW_DIRECTORY}/{RAW_SUBDIRECTORY}/{digest[:2]}/{digest}.eml"
        target = self._root / key
        if target.exists():
            self._verify_existing(target, raw, digest)
            return RawMailObject(sha256=digest, storage_key=key, size_bytes=len(raw))
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
            raise MailRawStorageError(
                f"could not store the raw message ({type(exc).__name__})"
            ) from exc
        return RawMailObject(sha256=digest, storage_key=key, size_bytes=len(raw))

    def _read_sync(self, storage_key: str) -> bytes:
        path = self._root / storage_key
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise MailRawStorageError(
                f"could not read the raw message ({type(exc).__name__})"
            ) from exc
        if hashlib.sha256(raw).hexdigest() != Path(storage_key).stem:
            raise MailRawStorageError("a stored raw message does not match its content address")
        return raw

    def _verify_existing(self, target: Path, raw: bytes, digest: str) -> None:
        """Refuse to reuse an object whose bytes disagree with its name."""
        try:
            existing = target.read_bytes()
        except OSError as exc:
            raise MailRawStorageError(
                f"could not read an existing raw message ({type(exc).__name__})"
            ) from exc
        if hashlib.sha256(existing).hexdigest() != digest or len(existing) != len(raw):
            raise MailRawStorageError(
                "an existing raw message does not match its content address"
            )


__all__ = ["RAW_DIRECTORY", "RAW_SUBDIRECTORY", "RawMailObject", "RawMailStore"]
