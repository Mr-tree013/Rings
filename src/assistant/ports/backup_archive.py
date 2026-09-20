"""BackupArchive port: write and read one `.gab` archive (ADR-0031).

The application service decides *what* belongs in a backup; this port is how it hands that decision
to something that knows the zip format. Splitting them keeps the service free of the archive
implementation and keeps the archive adapter free of policy: it can write a manifest, list members
and extract one member to a path, and it cannot decide that an object should be skipped.

An `ArchiveHandle` is what reading produces. It carries the manifest and the member list, and it
extracts one member at a time — never `extractall`, because the caller has to be able to check every
name, size and hash before a byte lands anywhere.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from assistant.domain.backup import BackupManifest


class ArchiveHandle(Protocol):
    """An opened archive: its manifest, its members, and one-member extraction."""

    @property
    def path(self) -> Path:
        """The archive file this handle reads."""
        ...

    @property
    def manifest(self) -> BackupManifest:
        """The archive's parsed manifest."""
        ...

    @property
    def member_names(self) -> tuple[str, ...]:
        """Every member name, sorted."""
        ...

    def extract(self, name: str, destination: Path) -> tuple[str, int]:
        """Write one listed member to `destination`, verifying its hash.

        Returns:
            The member's SHA-256 and size in bytes.

        Raises:
            InvalidBackupArchive: the member is not listed, or its bytes do not match the manifest.
        """
        ...


class BackupArchive(Protocol):
    """Reads and writes the archive format."""

    def write(
        self,
        destination: Path,
        *,
        manifest: BackupManifest,
        database: Path,
        objects: tuple[tuple[str, Path], ...],
    ) -> Path:
        """Write one archive atomically, or leave nothing behind.

        Raises:
            InvalidBackupArchive: the destination exists, or a member name or source is unusable.
        """
        ...

    def read(self, path: Path) -> ArchiveHandle:
        """Validate and open one archive without extracting anything.

        Raises:
            InvalidBackupArchive: the file is not an archive, or its members or manifest are
                unusable, duplicated, unlisted or missing.
        """
        ...


__all__ = ["ArchiveHandle", "BackupArchive"]
