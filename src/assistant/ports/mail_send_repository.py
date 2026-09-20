"""MailSendRepository port: send links and reconciliation history (ADR-0024).

Two facts have to be atomic with each other or not exist at all:

- **a prepared action and its send link.** An action that snapshotted a draft without recording
  which version and which Message-ID it did so with cannot be reviewed or reconciled; and a link
  without its action would name a send that does not exist;
- **a resolved reconciliation.** Promoting a run to `SUCCEEDED`, marking the action `EXECUTED` and
  writing the audit row must happen together, or a crash could leave a success nobody recorded —
  or an audit row claiming a resolution that never happened.

Nothing here exposes a connection, a transaction or SQL.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from assistant.domain.action import ActionRequest, ActionRequestId
from assistant.domain.mail_draft import MailDraftId
from assistant.domain.mail_send import (
    MailSendLink,
    MailSendReconciliation,
)


class MailSendRepository(Protocol):
    """Durable send links and Sent-folder reconciliation history."""

    async def add_action_with_link(
        self, action: ActionRequest, link: MailSendLink
    ) -> ActionRequest:
        """Store a prepared send action and its link in one transaction.

        Raises:
            CommitmentStoreError: either row could not be stored; neither is written.
        """
        ...

    async def get_link(self, action_id: ActionRequestId) -> MailSendLink | None:
        """Return the link for one action, or `None` when it is not a mail send."""
        ...

    async def get_link_for_draft_version(
        self, draft_id: MailDraftId, draft_version: int
    ) -> MailSendLink | None:
        """Return the send link for one exact draft version, or `None`."""
        ...

    async def list_links(self, *, limit: int | None = 20) -> list[MailSendLink]:
        """List send links, newest first."""
        ...

    async def count_links(self) -> int:
        """How many mail send actions have ever been prepared."""
        ...

    async def record_reconciliation(
        self, reconciliation: MailSendReconciliation
    ) -> MailSendReconciliation:
        """Append one Sent-folder lookup to the audit trail."""
        ...

    async def list_reconciliations(
        self, action_id: ActionRequestId
    ) -> list[MailSendReconciliation]:
        """List one action's lookups, oldest first."""
        ...

    async def latest_reconciliation(
        self, action_id: ActionRequestId
    ) -> MailSendReconciliation | None:
        """Return the most recent lookup for one action, or `None`."""
        ...

    async def resolve_to_succeeded(
        self,
        *,
        action_id: ActionRequestId,
        execution_run_id: UUID,
        reconciliation: MailSendReconciliation,
    ) -> MailSendReconciliation:
        """Promote an unresolved run to SUCCEEDED, mark the action EXECUTED, record the lookup.

        All three happen in one transaction. The caller must already have decided that the exact
        approved Message-ID was found in the Sent mailbox.

        Raises:
            CommitmentStoreError: a row could not be written; nothing is changed.
        """
        ...


__all__ = ["MailSendRepository"]
