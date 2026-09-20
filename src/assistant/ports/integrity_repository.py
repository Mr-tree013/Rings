"""IntegrityRepository port: the read-only database audit an integrity check runs (ADR-0031).

One method, because the checker's job is one thing: look at the stored state and say what it found.
The implementation does the SQL; the application service composes the sections with the checks that
need the filesystem (content objects) or the configuration (knowledge roots).

Nothing here writes, migrates, repairs or re-hashes anything into the database. The audit reports;
a person decides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from assistant.domain.integrity import IntegritySection


@dataclass(frozen=True, slots=True)
class IntegrityObject:
    """One content object the database references, with the hash it recorded."""

    kind: str
    storage_key: str
    sha256: str


@dataclass(frozen=True, slots=True)
class ConfiguredRoot:
    """One configured storage root, as the catalog knows it."""

    root_id: str
    kind: str
    path: str = ""
    reachable: bool = True


@dataclass(frozen=True, slots=True)
class DatabaseAudit:
    """What one read-only database audit produced."""

    sections: tuple[IntegritySection, ...] = field(default_factory=tuple)
    objects: tuple[IntegrityObject, ...] = field(default_factory=tuple)
    roots: tuple[ConfiguredRoot, ...] = field(default_factory=tuple)
    applied_migrations: tuple[str, ...] = ()


class IntegrityRepository(Protocol):
    """A read-only audit of the durable runtime state."""

    def audit(self) -> DatabaseAudit:
        """Return the database sections, the referenced content objects and the configured roots."""
        ...


__all__ = [
    "ConfiguredRoot",
    "DatabaseAudit",
    "IntegrityObject",
    "IntegrityRepository",
]
