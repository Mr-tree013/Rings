"""Reading the permission bits of a private runtime object (ADR-0032).

The application layer needs to *report* unsafe modes without owning the platform call that reads
them, and without being able to change anything: this port is read-only by construction, because it
has no method that writes. The adapter that implements it is `adapters/runtime/permissions.py`,
which is also what the stores use when they create an object.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ObjectMode:
    """What the filesystem reports about one object's permissions."""

    mode: int | None
    """Unix permission bits, or `None` when this platform or filesystem does not report them."""

    world_writable: bool
    """Whether anybody on the machine may write to it."""

    owner_only: bool
    """Whether it has no group or other bits at all."""


class FileModeInspector(Protocol):
    """Read-only access to permission bits."""

    def inspect(self, path: Path) -> ObjectMode:
        """Report the modes of `path`. Never changes them."""
        ...


__all__ = ["FileModeInspector", "ObjectMode"]
