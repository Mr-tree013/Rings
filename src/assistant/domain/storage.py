"""Stable storage identity for local folders and archive vaults (ADR-0011).

A document is identified by *where it lives logically* — a storage root id plus a path
relative to that root — never by the mount point it happens to be visible at today:

```text
local://university/SoftwareEngineering/Lab1/report.pdf
vault://archive-main/Courses/2025/OS/Lab2/report.pdf
```

`StorageRoot` deliberately has no physical path: `/mnt/e/...` or `E:\\...` is a runtime
location, not an identity. The current location lives in the catalog's runtime metadata.

This module is pure: no I/O, no `os`, no `pathlib.Path` — enforced by the architecture
tests. `PurePosixPath` is used only as a value object.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urlsplit

from assistant.domain.errors import InvalidStorageRoot, InvalidStorageUri

ROOT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
"""Root ids are lowercase, hyphen-separated, and at most 63 characters."""

_WINDOWS_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


class StorageKind(StrEnum):
    """Where a document lives: a folder on this machine, or an archive vault."""

    LOCAL = "local"
    VAULT = "vault"


def validate_root_id(root_id: str) -> str:
    """Return `root_id` when it is a valid stable identity, else raise.

    Valid: `archive-main`, `university`, `photos-2024`.
    Invalid: `../usb`, `A B`, `/mnt/e`, `Archive`, `-leading`.
    """
    if not ROOT_ID_PATTERN.match(root_id):
        raise InvalidStorageRoot(
            f"invalid storage root id {root_id!r}: expected {ROOT_ID_PATTERN.pattern}"
        )
    return root_id


def validate_relative_path(relative_path: str) -> str:
    """Return a normalised POSIX relative path, or raise for anything unsafe.

    Rejected: empty paths, absolute paths, `.`/`..` segments, backslashes, Windows drive
    prefixes, and empty segments (`a//b`).
    """
    if not relative_path or not relative_path.strip():
        raise InvalidStorageUri("relative path must not be empty")
    if "\\" in relative_path:
        raise InvalidStorageUri("relative path must use POSIX separators, not backslashes")
    if relative_path.startswith("/"):
        raise InvalidStorageUri("relative path must not be absolute")
    segments = relative_path.split("/")
    for segment in segments:
        if segment in {"", ".", ".."}:
            raise InvalidStorageUri(f"relative path must not contain a {segment!r} segment")
        if _WINDOWS_DRIVE_PREFIX.match(segment):
            raise InvalidStorageUri("relative path must not contain a Windows drive prefix")
    return "/".join(segments)


def _quote_path(relative_path: str) -> str:
    """Percent-encode a relative path, keeping `/` as the separator."""
    return quote(relative_path, safe="/")


@dataclass(frozen=True, slots=True)
class StorageRoot:
    """A stable, path-independent storage identity."""

    root_id: str
    kind: StorageKind
    label: str

    def __post_init__(self) -> None:
        validate_root_id(self.root_id)
        if not self.label.strip():
            raise InvalidStorageRoot("storage root label must not be blank")

    @property
    def scheme(self) -> str:
        """The URI scheme that identifies this root kind."""
        return self.kind.value


@dataclass(frozen=True, slots=True)
class StorageUri:
    """A stable logical document reference: `<kind>://<root-id>/<relative-path>`."""

    kind: StorageKind
    root_id: str
    relative_path: str

    def __post_init__(self) -> None:
        validate_root_id(self.root_id)
        validate_relative_path(self.relative_path)

    def __str__(self) -> str:
        return f"{self.kind.value}://{self.root_id}/{_quote_path(self.relative_path)}"

    @classmethod
    def parse(cls, value: str) -> StorageUri:
        """Parse a logical URI, rejecting anything that could escape its root."""
        split = urlsplit(value)
        try:
            kind = StorageKind(split.scheme)
        except ValueError as exc:
            raise InvalidStorageUri(f"unsupported storage scheme: {split.scheme!r}") from exc
        if not split.netloc:
            raise InvalidStorageUri("storage URI must name a storage root")
        if split.query or split.fragment:
            raise InvalidStorageUri("storage URI must not carry a query or fragment")
        if not split.path.startswith("/"):
            raise InvalidStorageUri("storage URI must contain a relative path")
        # Decode exactly once: validation then runs on the decoded text, so an encoded
        # `..%2F`-style trick cannot slip past as a literal segment.
        decoded = unquote(split.path[1:])
        return cls(kind=kind, root_id=split.netloc, relative_path=decoded)

    @property
    def name(self) -> str:
        """The final path segment."""
        return PurePosixPath(self.relative_path).name

    @property
    def suffix(self) -> str:
        """The final suffix, or an empty string."""
        return PurePosixPath(self.relative_path).suffix


__all__ = [
    "ROOT_ID_PATTERN",
    "StorageKind",
    "StorageRoot",
    "StorageUri",
    "validate_relative_path",
    "validate_root_id",
]

