"""The single-instance guarantee for `assistantd` (ADR-0032).

One runtime data root has exactly one daemon. The authority for that claim is an advisory lock the
kernel holds — `fcntl.flock(fd, LOCK_EX | LOCK_NB)` on `<runtime>/assistantd.lock` — because the
kernel releases it when the process dies, however it dies. The file's *existence* proves nothing:
a lock file left behind by a machine that lost power is just an old file, and treating it as a lock
would mean a daemon that refuses to start until somebody deletes something.

The metadata written inside the lock file (pid, start time, version) is a courtesy for
`pw daemon status`. It is never consulted to decide whether a daemon is running, and it never
contains a credential, a token or a config value.
"""

from __future__ import annotations

import json
import os
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from assistant.adapters.runtime.permissions import (
    ensure_private_directory,
    ensure_private_file,
)

LOCK_FILENAME = "assistantd.lock"


class InstanceLockHeld(RuntimeError):
    """Another process holds the daemon lock for this runtime data root."""

    def __init__(self, path: Path, metadata: dict[str, object] | None = None) -> None:
        self.path = path
        self.metadata = metadata or {}
        super().__init__(
            "Another assistantd instance is already running for this runtime data directory."
        )


def daemon_lock_path(runtime_root: Path) -> Path:
    """Where the daemon lock lives for `runtime_root`."""
    return Path(runtime_root) / LOCK_FILENAME


@dataclass(frozen=True, slots=True)
class LockState:
    """What one attempt to take the lock found."""

    held: bool
    metadata: dict[str, object]
    """Diagnostic contents of the lock file; empty when there is no readable metadata."""


def read_lock_metadata(path: Path) -> dict[str, object]:
    """Read the informational metadata beside a lock, tolerating anything unreadable."""
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


class InstanceLock(AbstractContextManager["InstanceLock"]):
    """An advisory, exclusive, process-lifetime lock on one file.

    Usage::

        with InstanceLock(path) as lock:
            ...  # exactly one process is here

    `acquire()` raises `InstanceLockHeld` when another *live* process holds the lock; a stale file
    is not an obstacle. On release the descriptor is closed, which drops the lock.
    """

    def __init__(
        self,
        path: Path,
        *,
        metadata: dict[str, object] | None = None,
    ) -> None:
        self._path = Path(path)
        self._metadata = dict(metadata or {})
        self._handle: int | None = None

    @property
    def path(self) -> Path:
        """The lock file this instance uses."""
        return self._path

    @property
    def held(self) -> bool:
        """Whether this instance currently holds the lock."""
        return self._handle is not None

    def acquire(self) -> InstanceLock:
        """Take the lock, or raise `InstanceLockHeld` if a live process has it.

        Raises:
            InstanceLockHeld: another process holds the lock right now.
            RuntimeError: this instance already holds it, or the platform has no `flock`.
        """
        if self._handle is not None:
            raise RuntimeError("this lock is already held by this instance")
        import fcntl

        ensure_private_directory(self._path.parent)
        handle = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        ensure_private_file(self._path)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            existing = read_lock_metadata(self._path)
            os.close(handle)
            raise InstanceLockHeld(self._path, existing) from exc
        self._handle = handle
        self._write_metadata()
        return self

    def release(self) -> None:
        """Drop the lock. Safe to call when it is not held."""
        if self._handle is None:
            return
        handle, self._handle = self._handle, None
        try:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError:  # pragma: no cover - closing the descriptor releases it anyway
            pass
        finally:
            os.close(handle)

    def _write_metadata(self) -> None:
        """Write the diagnostic header. Failure here never affects the lock."""
        handle = self._handle
        if handle is None or not self._metadata:
            return
        try:
            payload = json.dumps(self._metadata, sort_keys=True, separators=(",", ":"))
            os.ftruncate(handle, 0)
            os.lseek(handle, 0, os.SEEK_SET)
            os.write(handle, payload.encode("utf-8"))
            os.fsync(handle)
        except OSError:  # pragma: no cover - a read-only or full filesystem
            return

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def inspect_lock(path: Path) -> LockState:
    """Ask whether some live process holds the daemon lock at `path`.

    The answer comes from trying to take the same advisory lock: success means nobody holds it, and
    the lock is released again immediately. The lock file's metadata is returned either way, marked
    as informational by its caller. A lock file that does not exist is reported as not held without
    creating anything: a status command must not have side effects.
    """
    if not Path(path).exists():
        return LockState(held=False, metadata={})
    probe = InstanceLock(path)
    try:
        probe.acquire()
    except InstanceLockHeld:
        return LockState(held=True, metadata=read_lock_metadata(path))
    else:
        probe.release()
        return LockState(held=False, metadata=read_lock_metadata(path))


__all__ = [
    "LOCK_FILENAME",
    "InstanceLock",
    "InstanceLockHeld",
    "LockState",
    "daemon_lock_path",
    "inspect_lock",
    "read_lock_metadata",
]
