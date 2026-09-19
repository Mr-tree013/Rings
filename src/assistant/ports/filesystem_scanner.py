"""FilesystemScanner port: turn a directory tree into a metadata snapshot (ADR-0011).

Scanning is blocking I/O, so the port is async and adapters offload the whole traversal to
a worker thread. The snapshot is metadata only — no file contents are opened, and symbolic
links are never followed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from assistant.domain.catalog import FilesystemSnapshot


class FilesystemScanner(Protocol):
    """Produces a metadata snapshot of one directory tree."""

    async def scan(self, path: Path) -> FilesystemSnapshot:
        """Scan `path` and return what was seen.

        Implementations must not raise for individual unreadable directories or files:
        such failures belong in `FilesystemSnapshot.errors` with `complete=False`, so the
        catalog can refuse to draw conclusions from a partial view.
        """
        ...


__all__ = ["FilesystemScanner"]

