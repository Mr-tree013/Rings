"""Resolving an unresolved send by looking in the Sent mailbox (ADR-0024).

```text
pw mail send reconcile ACTION
        │
        ├── the action must be mail.send with a RUNNING or UNKNOWN attempt
        ├── the approved Message-ID must equal the linked one (re-derived, not trusted)
        ▼
read-only Sent lookup for exactly that Message-ID
        ├── FOUND     ──► run SUCCEEDED + action EXECUTED + audit row   (one transaction)
        ├── NOT_FOUND ──► audit row only: the attempt stays unresolved
        ├── AMBIGUOUS ──► audit row only: two messages carry the id, so nobody guesses
        └── UNAVAILABLE ─► audit row only: no mailbox, no credential, or a transport failure
```

Three rules are the whole point:

- **a found message proves delivery.** The Sent mailbox holding this exact Message-ID is evidence
  that the server accepted the message, so the attempt can honestly be called `SUCCEEDED`;
- **a missing message proves nothing.** Sent folders are not guaranteed to exist, to be readable,
  or to be written before this lookup runs. `NOT_FOUND` is recorded and the attempt stays
  unresolved;
- **nothing here sends.** There is no resend path, no automatic retry and no way to turn an
  unresolved attempt into a second message. A user who wants a different outcome must cancel this
  action, prepare a new one and approve it.

The lookup is explicit and user-triggered in V1. No daemon, worker or schedule calls it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from assistant.domain.action import ActionRequest, ActionRequestId
from assistant.domain.config import MailAccountConfig
from assistant.domain.errors import (
    ActionRequestNotFound,
    MailAuthenticationError,
    MailConnectionError,
    MailCredentialsMissing,
    MailProtocolError,
    MailSendNotReconcilable,
)
from assistant.domain.execution import ExecutionRun, ExecutionRunStatus
from assistant.domain.mail_send import (
    MailSendLink,
    MailSendPayload,
    MailSendReconciliation,
    MailSendReconciliationResult,
    new_mail_send_reconciliation_id,
)
from assistant.ports.action_repository import ActionRepository
from assistant.ports.clock import Clock
from assistant.ports.mail_send_repository import MailSendRepository
from assistant.ports.sent_mail_lookup import (
    SentMailLookup,
    SentMailLookupOutcome,
    SentMailMatch,
)

LOGGER = logging.getLogger("assistant.mail")


@dataclass(frozen=True, slots=True)
class MailSendReconciliationOutcome:
    """What one reconciliation concluded, and whether the action's state changed.

    `reconciliation` is the audit row written by this call, or the most recent one when the
    attempt was already resolved and no new lookup was needed.
    """

    result: MailSendReconciliationResult
    reconciliation: MailSendReconciliation | None = None
    run: ExecutionRun | None = None
    resolved: bool = False
    already_sent: bool = False


class MailSendReconciliationService:
    """Looks for one approved message in the Sent mailbox, and refuses to guess."""

    def __init__(
        self,
        actions: ActionRepository,
        send: MailSendRepository,
        clock: Clock,
        *,
        lookup: SentMailLookup | None = None,
        accounts: tuple[MailAccountConfig, ...] = (),
    ) -> None:
        self._actions = actions
        self._send = send
        self._clock = clock
        self._lookup = lookup
        self._accounts = {account.id: account for account in accounts}

    async def reconcile(self, action_id: ActionRequestId | str) -> MailSendReconciliationOutcome:
        """Check whether the approved message reached the Sent mailbox.

        Raises:
            ActionRequestNotFound: no such action.
            MailSendNotReconcilable: the action is not a mail send, has no link, has no attempt,
                or is not in a state a lookup could resolve.
        """
        action = await self._require_send_action(action_id)
        link = await self._send.get_link(action.id)
        if link is None:
            raise MailSendNotReconcilable(action.id, "it has no send link")
        payload = MailSendPayload.from_payload(action.payload)
        _require_same_message_id(payload, link)
        run = await self._actions.latest_execution(action.id)
        if run is None:
            raise MailSendNotReconcilable(action.id, "no execution has been attempted")
        if run.status is ExecutionRunStatus.SUCCEEDED:
            # Already resolved: report it without touching a mailbox again, and without inventing
            # an audit row for a lookup that did not happen.
            return MailSendReconciliationOutcome(
                result=MailSendReconciliationResult.FOUND,
                reconciliation=await self._send.latest_reconciliation(action.id),
                run=run,
                resolved=True,
                already_sent=True,
            )
        if not run.blocks_retry:
            raise MailSendNotReconcilable(
                action.id,
                f"its latest attempt ended as {run.status.value}, which a Sent lookup cannot "
                "improve",
            )
        result, location = await self._look(payload)
        row = _audit_row(action.id, run, self._clock.now(), result, location=location)
        if result is MailSendReconciliationResult.FOUND:
            stored = await self._send.resolve_to_succeeded(
                action_id=action.id, execution_run_id=run.id, reconciliation=row
            )
            LOGGER.info("mail send reconciliation confirmed delivery")
            resolved_run = await self._actions.get_execution(run.id)
            return MailSendReconciliationOutcome(
                result=result, reconciliation=stored, run=resolved_run, resolved=True
            )
        await self._send.record_reconciliation(row)
        LOGGER.info("mail send reconciliation recorded result=%s", result.value)
        return MailSendReconciliationOutcome(result=result, reconciliation=row, run=run)

    # ------------------------------------------------------------------ internals

    async def _require_send_action(
        self, action_id: ActionRequestId | str
    ) -> ActionRequest:
        resolved = (
            action_id
            if not isinstance(action_id, str)
            else await self._actions.resolve_action_id(action_id)
        )
        action = await self._actions.get_action(resolved)
        if action is None:
            raise ActionRequestNotFound(action_id)
        if action.action_type.value != "mail.send":
            raise MailSendNotReconcilable(
                action.id, f"its action type is {action.action_type.value}"
            )
        return action

    async def _look(
        self, payload: MailSendPayload
    ) -> tuple[MailSendReconciliationResult, SentMailMatch | None]:
        """Ask the Sent mailbox, mapping every possible failure to a recorded outcome."""
        account = self._accounts.get(payload.account_id)
        if account is None or not (account.sent_mailbox or "").strip():
            return MailSendReconciliationResult.UNAVAILABLE, None
        if self._lookup is None:
            return MailSendReconciliationResult.UNAVAILABLE, None
        try:
            found = await self._lookup.find_message(
                mailbox_name=account.sent_mailbox or "",
                rfc_message_id=payload.rfc_message_id,
            )
        except (MailCredentialsMissing, MailAuthenticationError) as exc:
            LOGGER.warning("mail send reconciliation unavailable: %s", type(exc).__name__)
            return MailSendReconciliationResult.UNAVAILABLE, None
        except (MailConnectionError, MailProtocolError) as exc:
            LOGGER.warning("mail send reconciliation unavailable: %s", type(exc).__name__)
            return MailSendReconciliationResult.UNAVAILABLE, None
        if found.outcome is SentMailLookupOutcome.FOUND:
            return MailSendReconciliationResult.FOUND, found.match
        if found.outcome is SentMailLookupOutcome.AMBIGUOUS:
            return MailSendReconciliationResult.AMBIGUOUS, None
        return MailSendReconciliationResult.NOT_FOUND, None


def _require_same_message_id(payload: MailSendPayload, link: MailSendLink) -> None:
    """The searched id must be the one that was approved and linked."""
    if payload.rfc_message_id != link.rfc_message_id:
        raise MailSendNotReconcilable(
            link.action_id,
            "the linked Message-ID does not match the approved payload",
        )


def _audit_row(
    action_id: ActionRequestId,
    run: ExecutionRun,
    checked_at: datetime,
    result: MailSendReconciliationResult,
    *,
    location: SentMailMatch | None = None,
) -> MailSendReconciliation:
    """One audit row. Only a FOUND result names a mailbox and a UID."""
    return MailSendReconciliation(
        id=new_mail_send_reconciliation_id(),
        action_id=action_id,
        execution_run_id=run.id,
        result=result,
        checked_at=checked_at,
        mailbox_name=None if location is None else location.mailbox_name,
        uidvalidity=None if location is None else location.uidvalidity,
        uid=None if location is None else location.uid,
    )


__all__ = [
    "MailSendReconciliationOutcome",
    "MailSendReconciliationService",
]
