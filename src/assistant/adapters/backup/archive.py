"""Writing and reading `.gab` archives, with the zip treated as hostile input (ADR-0031).

Writing is atomic and refuses to clobber: the archive is built in a temporary file in the same
directory, flushed, `fsync`ed, verified and only then renamed onto the requested name, so a failed
backup leaves no half-file behind and an existing backup is never overwritten.

Reading is where the care is. `open_archive` walks the archive's central directory and refuses, in
this order:

- **the archive ends exactly where it says it ends.** A ZIP may legally carry a comment and a reader
  may legally ignore bytes after the end-of-central-directory record; a backup may not. The final
  EOCD record must sit at the very end of the file with a zero comment length, and the central
  directory it points at must end exactly where that record begins. `valid.gab + b"junk"`,
  `valid.gab + another-archive` and an archive with a comment are all invalid (ADR-0032);
- a member count beyond the limit, or a manifest beyond its size limit;
- a name that is absolute, traverses with `..`, uses backslashes, carries a drive letter, has an
  empty segment or contains a NUL byte;
- a name that is not one of the four allowed kinds, or a top-level name the format does not have;
- a duplicate name;
- a symbolic link or other non-regular member;
- a declared size that is negative, beyond a single object's limit, or beyond the total limit, or a
  compression ratio that suggests a zip bomb.

Only afterwards does it parse the manifest and check that the listed objects and the present
members are the same set — in both directions, so an unlisted member is as much a refusal as a
missing one.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from assistant.adapters.runtime.permissions import (
    ensure_private_directory,
    ensure_private_file,
)
from assistant.domain.backup import (
    DATABASE_MEMBER,
    MANIFEST_MEMBER,
    MAX_COMPRESSION_RATIO,
    MAX_DATABASE_BYTES,
    MAX_MANIFEST_BYTES,
    MAX_MEMBERS,
    MAX_OBJECT_BYTES,
    MAX_TOTAL_UNCOMPRESSED_BYTES,
    BackupManifest,
    is_allowed_member,
)
from assistant.domain.errors import InvalidBackupArchive

CHUNK_BYTES = 1024 * 1024
"""How much of a member is read at once, so nothing is ever fully in memory."""

_EOCD_SIGNATURE = b"PK\x05\x06"
_ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_EOCD_LENGTH = 22
_ZIP64_EOCD_MINIMUM_LENGTH = 56
_ZIP64_LOCATOR_LENGTH = 20
_MAX_COMMENT_BYTES = 0xFFFF
_TAIL_BYTES = _MAX_COMMENT_BYTES + _EOCD_LENGTH


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    """One member of an archive, as the central directory describes it."""

    name: str
    size_bytes: int
    compressed_bytes: int


@dataclass(frozen=True, slots=True)
class ArchiveContents:
    """An opened archive: its manifest and the members it actually contains."""

    path: Path
    manifest: BackupManifest
    entries: tuple[ArchiveEntry, ...] = field(default_factory=tuple)

    @property
    def entry_names(self) -> tuple[str, ...]:
        """Every member name, sorted."""
        return tuple(sorted(entry.name for entry in self.entries))

    def entry_for(self, name: str) -> ArchiveEntry | None:
        """The entry with this name, or `None`."""
        for entry in self.entries:
            if entry.name == name:
                return entry
        return None

    @property
    def member_names(self) -> tuple[str, ...]:
        """Every member name, sorted. The port's name for the same list."""
        return self.entry_names

    def extract(self, name: str, destination: Path) -> tuple[str, int]:
        """Extract one listed member, verifying it as it is written."""
        return extract_member(self, name, destination)


class ZipBackupArchive:
    """The `.gab` writer and reader, as the port describes it."""

    def write(
        self,
        destination: Path,
        *,
        manifest: BackupManifest,
        database: Path,
        objects: tuple[tuple[str, Path], ...],
    ) -> Path:
        """Write one archive atomically, or leave nothing behind."""
        return make_archive(
            destination, manifest=manifest, database=database, objects=objects
        )

    def read(self, path: Path) -> ArchiveContents:
        """Validate and open one archive without extracting anything."""
        return open_archive(path)


def make_archive(
    destination: Path,
    *,
    manifest: BackupManifest,
    database: Path,
    objects: tuple[tuple[str, Path], ...],
) -> Path:
    """Write one archive atomically, or leave nothing behind.

    Raises:
        InvalidBackupArchive: the destination exists, a member name is unusable, or a source file is
            missing.
    """
    target = Path(destination)
    if target.exists():
        raise InvalidBackupArchive(
            f"{target} already exists; this project never overwrites a backup"
        )
    members: list[tuple[str, Path]] = [(DATABASE_MEMBER, Path(database))] + [
        (name, Path(path)) for name, path in objects
    ]
    seen: set[str] = set()
    for name, path in members:
        # The manifest is written from the manifest object itself, so it may never arrive here as a
        # file; the database member and the content objects are exactly what this function writes.
        if not is_allowed_member(name) or name == MANIFEST_MEMBER:
            raise InvalidBackupArchive(f"refusing to write member {name!r}")
        if name in seen:
            raise InvalidBackupArchive(f"refusing to write member {name!r} twice")
        seen.add(name)
        if not path.is_file():
            raise InvalidBackupArchive(f"the source for {name!r} is missing")
    ensure_private_directory(target.parent)
    handle, temporary = tempfile.mkstemp(dir=str(target.parent), suffix=".part")
    os.close(handle)
    temporary_path = Path(temporary)
    ensure_private_file(temporary_path)
    try:
        with zipfile.ZipFile(temporary_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            info = zipfile.ZipInfo(MANIFEST_MEMBER)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, manifest.to_json())
            for name, path in members:
                archive.write(path, arcname=name)
        _fsync_file(temporary_path)
        open_archive(temporary_path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    os.replace(temporary_path, target)
    ensure_private_file(target)
    _fsync_directory(target.parent)
    return target


def open_archive(path: Path) -> ArchiveContents:
    """Validate and read one archive's structure and manifest, without extracting anything.

    Raises:
        InvalidBackupArchive: the file is not a zip, a member name or size is unusable, the manifest
            is malformed, or the manifest and the members disagree.
    """
    source = Path(path)
    if not source.is_file():
        raise InvalidBackupArchive(f"{source} is not a file")
    _require_strict_end_of_archive(source)
    try:
        archive = zipfile.ZipFile(source, "r")
    except zipfile.BadZipFile as exc:
        raise InvalidBackupArchive("the backup is not a readable zip archive") from exc
    with archive:
        infos = archive.infolist()
        _require_archive_shape(infos)
        manifest_info = _manifest_info(infos)
        raw_manifest = _read_member(archive, manifest_info, limit=MAX_MANIFEST_BYTES)
        manifest = BackupManifest.from_json(raw_manifest)
        entries = tuple(
            ArchiveEntry(
                name=info.filename,
                size_bytes=info.file_size,
                compressed_bytes=info.compress_size,
            )
            for info in infos
        )
        _require_manifest_matches_members(manifest, entries)
        return ArchiveContents(path=source, manifest=manifest, entries=entries)


def extract_member(contents: ArchiveContents, name: str, destination: Path) -> tuple[str, int]:
    """Extract one listed member into `destination`, verifying its hash as it is written.

    Returns:
        The member's SHA-256 and size in bytes.

    Raises:
        InvalidBackupArchive: the member is not listed, or its bytes do not match the manifest.
    """
    described = contents.manifest.object_for(name)
    if described is None and name != DATABASE_MEMBER:
        raise InvalidBackupArchive(f"{name!r} is not listed in the manifest")
    if name == DATABASE_MEMBER:
        expected = contents.manifest.database_sha256
    elif described is not None:
        expected = described.sha256
    else:  # pragma: no cover - the branch above already refused this
        raise InvalidBackupArchive(f"{name!r} is not listed in the manifest")
    target = Path(destination)
    ensure_private_directory(target.parent)
    digest = hashlib.sha256()
    written = 0
    with zipfile.ZipFile(contents.path, "r") as archive:
        info = _info_for(archive, name)
        with archive.open(info, "r") as stream, target.open("wb") as output:
            while True:
                chunk = stream.read(CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
                written += len(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
    ensure_private_file(target)
    if digest.hexdigest() != expected:
        target.unlink(missing_ok=True)
        raise InvalidBackupArchive(f"{name!r} does not match the hash recorded in the manifest")
    if described is not None and written != described.size_bytes:
        target.unlink(missing_ok=True)
        raise InvalidBackupArchive(
            f"{name!r} is {written} bytes but the manifest says {described.size_bytes}"
        )
    return digest.hexdigest(), written


def _require_archive_shape(infos: list[zipfile.ZipInfo]) -> None:
    """Refuse an archive whose members are unsafe, duplicated, linked or too large."""
    if not infos:
        raise InvalidBackupArchive("the archive is empty")
    if len(infos) > MAX_MEMBERS:
        raise InvalidBackupArchive(
            f"the archive declares {len(infos)} members, beyond the {MAX_MEMBERS} limit"
        )
    seen: set[str] = set()
    total = 0
    for info in infos:
        name = info.filename
        if not is_allowed_member(name):
            raise InvalidBackupArchive(f"the archive contains an unusable member {name!r}")
        if name in seen:
            raise InvalidBackupArchive(f"the archive contains {name!r} twice")
        seen.add(name)
        mode = info.external_attr >> 16
        if mode and (mode & 0o170000) == 0o120000:
            raise InvalidBackupArchive(f"{name!r} is a symbolic link")
        if info.file_size < 0:
            raise InvalidBackupArchive(f"{name!r} declares a negative size")
        limit = _limit_for(name)
        if info.file_size > limit:
            raise InvalidBackupArchive(
                f"{name!r} declares {info.file_size} bytes, beyond its {limit}-byte limit"
            )
        if info.compress_size > 0 and info.file_size > 0:
            ratio = info.file_size / info.compress_size
            if ratio > MAX_COMPRESSION_RATIO:
                raise InvalidBackupArchive(
                    f"{name!r} compresses {ratio:.0f}:1, which is not credible"
                )
        total += info.file_size
        if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise InvalidBackupArchive(
                "the archive's declared contents exceed the total size limit"
            )


def _limit_for(name: str) -> int:
    if name == MANIFEST_MEMBER:
        return MAX_MANIFEST_BYTES
    if name == DATABASE_MEMBER:
        return MAX_DATABASE_BYTES
    return MAX_OBJECT_BYTES


def _manifest_info(infos: list[zipfile.ZipInfo]) -> zipfile.ZipInfo:
    for info in infos:
        if info.filename == MANIFEST_MEMBER:
            return info
    raise InvalidBackupArchive("the archive has no manifest")


def _info_for(archive: zipfile.ZipFile, name: str) -> zipfile.ZipInfo:
    for info in archive.infolist():
        if info.filename == name:
            return info
    raise InvalidBackupArchive(f"{name!r} is missing from the archive")


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, *, limit: int) -> bytes:
    if info.file_size > limit:
        raise InvalidBackupArchive(f"{info.filename!r} is larger than {limit} bytes")
    with archive.open(info, "r") as stream:
        return stream.read(limit + 1)


def _require_manifest_matches_members(
    manifest: BackupManifest, entries: tuple[ArchiveEntry, ...]
) -> None:
    """The manifest and the archive must describe exactly the same set of objects."""
    present = {
        entry.name
        for entry in entries
        if entry.name not in (MANIFEST_MEMBER, DATABASE_MEMBER)
    }
    listed = set(manifest.mail_keys()) | set(manifest.web_keys())
    if present - listed:
        raise InvalidBackupArchive(
            f"the archive contains unlisted members: {sorted(present - listed)}"
        )
    if listed - present:
        raise InvalidBackupArchive(
            f"the manifest lists missing members: {sorted(listed - present)}"
        )
    if manifest.database_sha256 and not any(
        entry.name == DATABASE_MEMBER for entry in entries
    ):
        raise InvalidBackupArchive("the archive has no database member")


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:  # pragma: no cover - depends on the filesystem
        os.fsync(stream.fileno())


def _require_strict_end_of_archive(source: Path) -> None:
    """Refuse anything that is not exactly one ZIP archive ending at its own last byte.

    `zipfile` (like every ZIP reader) searches backwards for the end-of-central-directory record, so
    it will happily read an archive that carries a comment or has another file glued behind it. A
    `.gab` file is not a container for "the backup and whatever else": the format promises that the
    bytes on disk *are* the state that was backed up. The check therefore reads only the tail of the
    file — enough to see the final EOCD record, its comment length and, for Zip64 archives, the
    locator and the Zip64 record it points at — and never parses the members itself.

    Raises:
        InvalidBackupArchive: the tail is missing, carries a comment, or is followed by more bytes.
    """
    size = source.stat().st_size
    if size < _EOCD_LENGTH:
        raise InvalidBackupArchive("the backup is not a readable zip archive")
    tail_length = min(size, _TAIL_BYTES)
    tail_start = size - tail_length
    with source.open("rb") as stream:
        stream.seek(tail_start)
        tail = stream.read(tail_length)
        position = tail.rfind(_EOCD_SIGNATURE)
        if position < 0:
            raise InvalidBackupArchive("the backup has no end-of-central-directory record")
        eocd_offset = tail_start + position
        record = tail[position : position + _EOCD_LENGTH]
        if len(record) < _EOCD_LENGTH:
            raise InvalidBackupArchive("the backup's end record is truncated")
        if int.from_bytes(record[20:22], "little") != 0:
            raise InvalidBackupArchive(
                "the backup archive carries a zip comment, which this format does not allow"
            )
        if eocd_offset + _EOCD_LENGTH != size:
            raise InvalidBackupArchive(
                "the backup archive has data after its end-of-central-directory record"
            )
        directory_size = int.from_bytes(record[12:16], "little")
        directory_offset = int.from_bytes(record[16:20], "little")
        if directory_offset == 0xFFFFFFFF or directory_size == 0xFFFFFFFF:
            _require_zip64_boundary(
                stream,
                tail=tail,
                tail_start=tail_start,
                eocd_offset=eocd_offset,
            )
            return
        if directory_offset + directory_size != eocd_offset:
            raise InvalidBackupArchive(
                "the backup archive's central directory does not end at its end record"
            )


def _require_zip64_boundary(
    stream: BinaryIO,
    *,
    tail: bytes,
    tail_start: int,
    eocd_offset: int,
) -> None:
    """The Zip64 form of the same boundary: locator, Zip64 record, central directory, end to end."""
    locator_offset = eocd_offset - _ZIP64_LOCATOR_LENGTH
    if locator_offset < tail_start:
        raise InvalidBackupArchive("the backup's Zip64 locator is not in the archive's tail")
    start = locator_offset - tail_start
    locator = tail[start : start + _ZIP64_LOCATOR_LENGTH]
    if not locator.startswith(_ZIP64_LOCATOR_SIGNATURE):
        raise InvalidBackupArchive("the backup's Zip64 end locator is missing")
    zip64_offset = int.from_bytes(locator[8:16], "little")
    stream.seek(zip64_offset)
    header = stream.read(_ZIP64_EOCD_MINIMUM_LENGTH)
    if len(header) < _ZIP64_EOCD_MINIMUM_LENGTH or not header.startswith(
        _ZIP64_EOCD_SIGNATURE
    ):
        raise InvalidBackupArchive("the backup's Zip64 end record is missing")
    record_size = int.from_bytes(header[4:12], "little")
    directory_size = int.from_bytes(header[40:48], "little")
    directory_offset = int.from_bytes(header[48:56], "little")
    if zip64_offset + 12 + record_size != locator_offset:
        raise InvalidBackupArchive(
            "the backup archive has data between its Zip64 end record and its end locator"
        )
    if directory_offset + directory_size != zip64_offset:
        raise InvalidBackupArchive(
            "the backup archive's central directory does not end at its Zip64 end record"
        )


def _fsync_directory(path: Path) -> None:  # pragma: no cover - depends on the filesystem
    """Flush the directory entry, so the rename survives a crash where the OS supports it."""
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


__all__ = [
    "ArchiveContents",
    "ArchiveEntry",
    "ZipBackupArchive",
    "extract_member",
    "make_archive",
    "open_archive",
]
