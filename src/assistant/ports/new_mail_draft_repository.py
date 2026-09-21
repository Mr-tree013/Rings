"""NewMailDraftRepository port: durable new-mail drafts (ADR-0037 §17).

An edit is a compare-and-set: the caller presents the version it read, and a mismatch is refused
rather than overwriting whatever changed in the meantime. The version is also what a prepared send
snapshots, so an edit always produces a new action rather than a second approval of old text.

Nothing here exposes a connection, a transaction or SQL.
"""

from __future__ import annotations

from typing import Protocol

from assistant.domain.new_mail_draft import NewMailDraft, NewMailDraftId


class NewMailDraftRepository(Protocol):
    """Durable new-mail drafts and their versions."""

    async def add_draft(self, draft: NewMailDraft) -> NewMailDraft:
        """Store a new draft.

        Raises:
            CommitmentStoreError: the draft could not be stored.
        """
        ...

    async def get_draft(self, draft_id: NewMailDraftId) -> NewMailDraft | None:
        """Return one draft, or `None`."""
        ...

    async def list_drafts(self, *, limit: int | None = 20) -> list[NewMailDraft]:
        """List drafts, newest update first, with a deterministic tie-break."""
        ...

    async def update_draft(
        self, draft: NewMailDraft, *, expected_version: int
    ) -> NewMailDraft:
        """Replace the editable fields of one draft, if it is still at `expected_version`.

        Raises:
            NewMailDraftNotFound: no such draft.
            StaleMailDraftUpdate: the stored version is not `expected_version`.
        """
        ...

    async def resolve_draft_id(self, reference: str) -> NewMailDraftId:
        """Resolve a full UUID or a unique prefix to a draft id.

        Raises:
            NewMailDraftNotFound: nothing matches.
            AmbiguousId: several drafts match.
        """
        ...


__all__ = ["NewMailDraftRepository"]
