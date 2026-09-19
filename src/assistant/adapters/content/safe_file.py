"""Reading catalogued files without trusting the catalogued path (ADR-0012).

A path is not safe just because it came from the catalog: between the metadata scan and the
content read, a regular file can be replaced by a symlink pointing anywhere. So every read
re-validates the path from scratch:

- the relative path must pass the same validation the storage layer uses;
- every path component is `lstat`ed and must not be a symbolic link;
- the final object must be a regular file (no FIFO, socket, device or directory);
- the opened file's size and mtime must match the catalog, before *and* after reading.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from assistant.domain.errors import (
    FileChangedDuringExtraction,
    InvalidStorageUri,
    UnsafeFilePath,
)
from assistant.domain.storage import validate_relative_path

_OPEN_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


@contextmanager
def open_catalogued_file(
    root_path: Path,
    relative_path: str,
    *,
    expected_size: int,
    expected_mtime_ns: int,
) -> Iterator[BinaryIO]:
    """Open a catalogued regular file for reading, or refuse.

    Raises:
        UnsafeFilePath: the path escapes the root, crosses a symlink, or is not a regular
            file.
        FileChangedDuringExtraction: the file does not match the catalog metadata, or it
            changed while it was being read.
    """
    try:
        safe_relative = validate_relative_path(relative_path)
    except InvalidStorageUri as exc:
        raise UnsafeFilePath(f"refusing to read {relative_path!r}: {exc}") from exc
    root = Path(root_path)
    if not root.is_dir():
        raise UnsafeFilePath(f"storage root {root} is not a usable directory")
    absolute, final_stat = _walk_without_symlinks(root, safe_relative)
    if not stat.S_ISREG(final_stat.st_mode):
        # Checked before opening: opening a FIFO for reading would block until a writer
        # appears, and devices are not documents either.
        raise UnsafeFilePath(f"{safe_relative} is not a regular file")
    descriptor = -1
    try:
        try:
            descriptor = os.open(absolute, os.O_RDONLY | _OPEN_NOFOLLOW)
        except OSError as exc:
            raise UnsafeFilePath(f"{safe_relative} could not be opened safely: {exc}") from exc
        stat_result = os.fstat(descriptor)
        if not stat.S_ISREG(stat_result.st_mode):
            raise UnsafeFilePath(f"{safe_relative} is not a regular file")
        _verify_unchanged(safe_relative, stat_result, expected_size, expected_mtime_ns)
        handle = os.fdopen(descriptor, "rb")
        descriptor = -1  # the file object owns the descriptor now
        try:
            yield handle
            _verify_unchanged(
                safe_relative,
                os.fstat(handle.fileno()),
                expected_size,
                expected_mtime_ns,
            )
        finally:
            handle.close()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _walk_without_symlinks(root: Path, relative_path: str) -> tuple[Path, os.stat_result]:
    current = root
    info: os.stat_result | None = None
    for component in relative_path.split("/"):
        current = current / component
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise UnsafeFilePath(f"{relative_path} could not be inspected: {exc}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise UnsafeFilePath(f"{relative_path} crosses a symlink at {component!r}")
    if info is None:  # pragma: no cover - validate_relative_path rejects empty paths
        raise UnsafeFilePath(f"{relative_path} is not a usable path")
    return current, info


def _verify_unchanged(
    relative_path: str,
    stat_result: os.stat_result,
    expected_size: int,
    expected_mtime_ns: int,
) -> None:
    if stat_result.st_size != expected_size or stat_result.st_mtime_ns != expected_mtime_ns:
        raise FileChangedDuringExtraction(
            f"{relative_path} changed since it was catalogued: "
            f"size {expected_size} -> {stat_result.st_size}, "
            f"mtime {expected_mtime_ns} -> {stat_result.st_mtime_ns}"
        )


def catalogued_file_matches_metadata(
    root_path: Path, relative_path: str, *, expected_size: int, expected_mtime_ns: int
) -> bool:
    """Stat-only revalidation: does the file on disk still match the catalog?

    Opens the file (no reads) to keep the same symlink and regular-file guarantees as a real
    read, and returns `False` instead of raising.
    """
    try:
        with open_catalogued_file(
            root_path,
            relative_path,
            expected_size=expected_size,
            expected_mtime_ns=expected_mtime_ns,
        ):
            return True
    except (UnsafeFilePath, FileChangedDuringExtraction):
        return False


__all__ = ["catalogued_file_matches_metadata", "open_catalogued_file"]
