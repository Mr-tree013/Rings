"""Metadata-only filesystem scanner (ADR-0011).

Rules that matter more than speed:

- **no file contents**: sizes and mtimes come from directory entries, so a vault full of
  photos or archives costs metadata reads only;
- **no symlinks**: file and directory symlinks are skipped and reported, so a scan can
  neither escape its root nor walk into a loop or a huge external tree;
- **never silently partial**: an unreadable directory or file downgrades the snapshot to
  `complete=False` with a bounded error, and the catalog then refuses to mark anything
  missing.
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
from pathlib import Path

from assistant.domain.catalog import FileSnapshotEntry, FilesystemSnapshot, ScanError
from assistant.domain.errors import InvalidStorageUri
from assistant.domain.storage import validate_relative_path
from assistant.domain.vault import MANIFEST_DIRECTORY
from assistant.ports.clock import Clock

EXCLUDED_ROOT_DIRECTORIES = frozenset({MANIFEST_DIRECTORY})
"""Internal directories that never enter the document catalog."""

MAX_ERROR_MESSAGE_LENGTH = 500


class FilesystemScanner:
    """`FilesystemScanner` implementation walking a tree with `os.scandir`."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    async def scan(self, path: Path) -> FilesystemSnapshot:
        return await asyncio.to_thread(self._scan_sync, Path(path))

    def _scan_sync(self, root: Path) -> FilesystemSnapshot:
        started_at = self._clock.now()
        entries: list[FileSnapshotEntry] = []
        errors: list[ScanError] = []
        skipped_symlinks: list[str] = []
        complete = True

        pending: list[tuple[Path, str]] = [(root, "")]
        while pending:
            directory, prefix = pending.pop()
            try:
                with os.scandir(directory) as iterator:
                    children = sorted(iterator, key=lambda child: child.name)
            except OSError as exc:
                complete = False
                errors.append(_scan_error(prefix, exc))
                continue
            for child in children:
                relative_path = child.name if not prefix else f"{prefix}/{child.name}"
                try:
                    if child.is_symlink():
                        skipped_symlinks.append(relative_path)
                        continue
                    if child.is_dir(follow_symlinks=False):
                        if not prefix and child.name in EXCLUDED_ROOT_DIRECTORIES:
                            continue
                        pending.append((Path(child.path), relative_path))
                        continue
                    if not child.is_file(follow_symlinks=False):
                        # Sockets, FIFOs and devices are not documents.
                        continue
                    validate_relative_path(relative_path)
                    stat_result = child.stat(follow_symlinks=False)
                    entries.append(
                        FileSnapshotEntry(
                            relative_path=relative_path,
                            name=child.name,
                            size_bytes=int(stat_result.st_size),
                            mtime_ns=int(stat_result.st_mtime_ns),
                            media_type=_media_type(child.name),
                        )
                    )
                except (OSError, InvalidStorageUri) as exc:
                    complete = False
                    errors.append(_scan_error(relative_path, exc))
        entries.sort(key=lambda entry: entry.relative_path)
        return FilesystemSnapshot(
            entries=tuple(entries),
            complete=complete,
            errors=tuple(errors),
            skipped_symlinks=tuple(sorted(skipped_symlinks)),
            started_at=started_at,
            finished_at=self._clock.now(),
        )


def _media_type(name: str) -> str | None:
    guessed, _encoding = mimetypes.guess_type(name, strict=False)
    return guessed


def _scan_error(relative_path: str, error: BaseException) -> ScanError:
    message = " ".join(str(error).split())
    if len(message) > MAX_ERROR_MESSAGE_LENGTH:
        message = message[: MAX_ERROR_MESSAGE_LENGTH - 3] + "..."
    return ScanError(
        relative_path=relative_path or ".",
        error_type=type(error).__name__,
        message=message,
    )


__all__ = [
    "EXCLUDED_ROOT_DIRECTORIES",
    "MAX_ERROR_MESSAGE_LENGTH",
    "FilesystemScanner",
]
