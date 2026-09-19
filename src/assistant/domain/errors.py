"""Errors that belong to the project's vocabulary.

They live in the domain layer because ports and store both speak them: application code
must be able to handle `DuplicateInboundEvent` without ever importing `sqlite3` or the
store implementation (ADR-0002, ADR-0008).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID

    from assistant.domain.inbound_event import EventStatus


class DomainError(Exception):
    """Base class for errors that are part of the project's vocabulary."""


class InvalidInboundEvent(DomainError):
    """An `InboundEvent` was built with values that break its invariants."""


class InvalidEventTransition(DomainError):
    """A status transition was attempted that the event state machine forbids."""

    def __init__(self, current: EventStatus, target: EventStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(f"inbound event cannot move from {current} to {target}")


class UnexpectedEventStatus(DomainError):
    """The stored status is not the one the caller declared it expected.

    This is the compare-and-set guard of `EventRepository.transition`: a mismatch means
    another writer moved the event first, so the caller's decision is stale.
    """

    def __init__(self, event_id: UUID, expected: EventStatus, actual: EventStatus) -> None:
        self.event_id = event_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"inbound event {event_id} is {actual}, but the caller expected {expected}"
        )


class DuplicateInboundEvent(DomainError):
    """An event with the same `(source, external_id)` identity already exists.

    Raised by `EventRepository.add` when the database-level uniqueness guarantee rejects
    a second record. The duplicate is never silently reported as a successful insert.
    """

    def __init__(self, source: str, external_id: str) -> None:
        self.source = source
        self.external_id = external_id
        super().__init__(f"inbound event {source!r}/{external_id!r} already exists")


class EventNotFound(DomainError):
    """No inbound event exists for the requested identity."""

    def __init__(self, event_id: UUID) -> None:
        self.event_id = event_id
        super().__init__(f"inbound event {event_id} does not exist")


class InvalidEventClaim(DomainError):
    """An `EventClaim` was built with values that break its invariants."""


class StaleEventClaim(DomainError):
    """A worker tried to finish work whose claim is no longer the current one.

    The lease expired and another worker reclaimed the event, or the event moved on in
    some other way. The caller must stop: its result belongs to a superseded attempt and
    may never overwrite the state of the newer claim (ADR-0010).
    """

    def __init__(self, event_id: UUID, claim_token: UUID) -> None:
        self.event_id = event_id
        self.claim_token = claim_token
        super().__init__(f"claim {claim_token} on inbound event {event_id} is no longer current")


class PermanentEventError(DomainError):
    """A handler failed in a way that retrying cannot fix.

    Raised by handlers to send an event straight to dead letter instead of consuming the
    remaining retry budget (ADR-0010).
    """


class InvalidStorageRoot(DomainError):
    """A storage root was declared with an invalid identity or label."""


class InvalidStorageUri(DomainError):
    """A logical storage URI is malformed, absolute, or tries to traverse out of its root."""


class StorageRootConflict(DomainError):
    """An existing root_id was reused with a different storage kind.

    A root's identity is permanent (ADR-0011): the same id cannot be a local folder in one
    scan and an archive vault in the next.
    """


class InvalidVaultManifest(DomainError):
    """A `.pa/vault.toml` is missing required fields, malformed, or self-contradictory."""


class VaultNotInitialized(DomainError):
    """The directory has no `.pa/vault.toml`, so it is not an archive vault yet.

    Initialisation is always explicit: seeing a removable drive must never create a
    manifest on its own.
    """


class VaultAlreadyInitialized(DomainError):
    """The directory already has a `.pa/vault.toml`; initialisation never overwrites it."""


class InvalidCatalogEntry(DomainError):
    """A catalog record, snapshot entry or scan result breaks its invariants."""


class InvalidSourceSpan(DomainError):
    """A source span is neither a usable line range nor a usable page number."""


class InvalidKnowledgeDocument(DomainError):
    """Extracted content or knowledge-index state breaks its invariants."""


class UnknownStorageRoot(DomainError):
    """The catalog has no such storage root."""


class UnknownCatalogEntry(DomainError):
    """The catalog has no such entry under that root."""


class StorageRootOffline(DomainError):
    """A storage root's physical location is not currently present.

    Offline is a runtime condition, never a catalog state: nothing is marked missing and no
    index metadata is discarded because a drive is unplugged.
    """


class StorageRootIdentityMismatch(DomainError):
    """The storage mounted at a root's known path is not the root we catalogued.

    A different vault id at the same mount point means the index (and any content read)
    would belong to someone else's data, so the operation is refused.
    """


class UnsafeFilePath(DomainError):
    """A path is unsafe to read: symlink, non-regular file, or escaping its root."""


class FileChangedDuringExtraction(DomainError):
    """The file no longer matches the catalog metadata that was used to plan the read."""


class ContentTooLarge(DomainError):
    """A file exceeds the size limit for its extractor."""


class ContentExtractionError(DomainError):
    """An extractor could not read a file it claims to support.

    Adapter-specific exceptions (for example pypdf's) are translated into this error so no
    third-party exception type crosses the adapter boundary.
    """


class Fts5Unavailable(DomainError):
    """This SQLite build has no FTS5 module."""


class TrigramTokenizerUnavailable(DomainError):
    """This SQLite build has FTS5 but not the trigram tokenizer."""


class KnowledgeIndexMismatch(DomainError):
    """An index database is bound to a different root or storage kind."""


class KnowledgeIndexNeedsRebuild(DomainError):
    """An index database was written by a schema version this build does not understand."""


class KnowledgeIndexCorrupt(DomainError):
    """An index database cannot be read.

    The index is derived data: recovery is a deliberate rebuild, never an automatic delete.
    """


__all__ = [
    "ContentExtractionError",
    "ContentTooLarge",
    "DomainError",
    "DuplicateInboundEvent",
    "EventNotFound",
    "FileChangedDuringExtraction",
    "Fts5Unavailable",
    "InvalidCatalogEntry",
    "InvalidEventClaim",
    "InvalidEventTransition",
    "InvalidInboundEvent",
    "InvalidKnowledgeDocument",
    "InvalidSourceSpan",
    "InvalidStorageRoot",
    "InvalidStorageUri",
    "InvalidVaultManifest",
    "KnowledgeIndexCorrupt",
    "KnowledgeIndexMismatch",
    "KnowledgeIndexNeedsRebuild",
    "PermanentEventError",
    "StaleEventClaim",
    "StorageRootConflict",
    "StorageRootIdentityMismatch",
    "StorageRootOffline",
    "TrigramTokenizerUnavailable",
    "UnexpectedEventStatus",
    "UnknownCatalogEntry",
    "UnknownStorageRoot",
    "UnsafeFilePath",
    "VaultAlreadyInitialized",
    "VaultNotInitialized",
]
