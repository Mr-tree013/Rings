"""MailDraftRepository port: durable local drafts and their knowledge provenance (ADR-0022).

A draft and the sources it cites are written together, in one transaction, because a draft whose
provenance is missing cannot be reviewed honestly: the reader would see a body that claims to be
grounded with nothing to check it against.

An edit is a compare-and-set: the caller presents the version it read, and a mismatch is refused
rather than overwriting whatever changed in the meantime.

Nothing here exposes a connection, a transaction or SQL.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from assistant.domain.mail_draft import (
    MailDraft,
    MailDraftId,
    MailDraftSource,
)


class MailDraftRepository(Protocol):
    """Durable reply drafts, their versions and their stored knowledge sources."""

    async def add_draft(
        self, draft: MailDraft, sources: Sequence[MailDraftSource] = ()
    ) -> MailDraft:
        """Store a new draft and its sources in one transaction.

        Raises:
            CommitmentStoreError: the draft could not be stored.
        """
        ...

    async def get_draft(self, draft_id: MailDraftId) -> MailDraft | None:
        """Return one draft, or `None`."""
        ...

    async def list_drafts(self, *, limit: int | None = 20) -> list[MailDraft]:
        """List drafts, newest update first, with a deterministic tie-break."""
        ...

    async def list_sources(self, draft_id: MailDraftId) -> list[MailDraftSource]:
        """List the knowledge sources stored for one draft, in `ordinal` order."""
        ...

    async def count_sources(self, draft_id: MailDraftId) -> int:
        """How many knowledge sources one draft cites."""
        ...

    async def update_draft(self, draft: MailDraft, *, expected_version: int) -> MailDraft:
        """Replace the editable fields of one draft, if it is still at `expected_version`.

        Only the subject, body, open questions, origin, version and `updated_at` are written: the
        recipients and the generation provenance are not editable in this phase.

        Raises:
            MailDraftNotFound: no such draft.
            StaleMailDraftUpdate: the stored version is not `expected_version`.
        """
        ...

    async def resolve_draft_id(self, reference: str) -> MailDraftId:
        """Resolve a full UUID or a unique prefix to a draft id.

        Raises:
            MailDraftNotFound: nothing matches.
            AmbiguousId: several drafts match.
        """
        ...

    async def count_drafts(self) -> int:
        """How many drafts are stored."""
        ...


__all__ = ["MailDraftRepository"]
