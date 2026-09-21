"""Durable new-mail drafts: write one, revise one, never send one (ADR-0037 §17-§18).

```text
compose(account, recipient, subject, body) ──► NewMailDraft version 1
revise(draft, subject, body, …)            ──► the same draft, version n+1
```

There is no model call here and no SMTP client: the *conversation* already carries the prose the
model wrote, and the *send* boundary is a separate, immutable `ActionRequest` prepared from a
draft version. Keeping this service that small is what makes "the model writes the words and
nothing else" checkable.
"""

from __future__ import annotations

from datetime import datetime

from assistant.domain.errors import NewMailDraftNotFound
from assistant.domain.new_mail_draft import NewMailDraft
from assistant.ports.clock import Clock
from assistant.ports.new_mail_draft_repository import NewMailDraftRepository

MAX_NEW_DRAFT_LISTING = 20
"""How many recent new-mail drafts one list may carry."""


class NewMailDraftService:
    """Creates, reads and revises local new-mail drafts. It cannot send anything."""

    def __init__(self, drafts: NewMailDraftRepository, clock: Clock) -> None:
        self._drafts = drafts
        self._clock = clock

    async def compose(
        self,
        *,
        account_id: str,
        to_address: str,
        subject: str,
        body_text: str,
        at: datetime | None = None,
    ) -> tuple[NewMailDraft, bool]:
        """Write one new-mail draft, at version 1.

        The flag says whether this call wrote it, so a caller can tell a fresh draft from a
        revision without guessing.

        Raises:
            InvalidNewMailDraft: the recipient, subject or body is unusable.
        """
        now = self._clock.now() if at is None else at
        draft = NewMailDraft(
            account_id=account_id,
            to_address=to_address,
            subject=subject,
            body_text=body_text,
            created_at=now,
            updated_at=now,
        )
        return await self._drafts.add_draft(draft), True

    async def revise(
        self,
        current: NewMailDraft,
        *,
        account_id: str | None = None,
        to_address: str | None = None,
        subject: str | None = None,
        body_text: str | None = None,
    ) -> NewMailDraft:
        """Store the next version of one draft, if nothing else changed it first.

        Raises:
            InvalidNewMailDraft: the new values break an invariant.
            StaleMailDraftUpdate: the stored draft is not at the version this caller read.
        """
        revised = current.revised(
            at=self._clock.now(),
            account_id=account_id,
            to_address=to_address,
            subject=subject,
            body_text=body_text,
        )
        return await self._drafts.update_draft(revised, expected_version=current.version)

    async def get(self, reference: str) -> NewMailDraft | None:
        """One draft by id or unique prefix, or `None` when nothing matches."""
        try:
            draft_id = await self._drafts.resolve_draft_id(reference)
        except NewMailDraftNotFound:
            return None
        return await self._drafts.get_draft(draft_id)

    async def require(self, reference: str) -> NewMailDraft:
        """One draft by id or unique prefix.

        Raises:
            NewMailDraftNotFound: nothing matches.
        """
        draft = await self.get(reference)
        if draft is None:
            raise NewMailDraftNotFound(reference)
        return draft

    async def list_recent(self, *, limit: int | None = MAX_NEW_DRAFT_LISTING) -> list[NewMailDraft]:
        """Recent drafts, newest update first."""
        return await self._drafts.list_drafts(limit=limit)


__all__ = ["MAX_NEW_DRAFT_LISTING", "NewMailDraftService"]
