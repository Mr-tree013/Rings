"""Inbound mail synchronization: IMAP in, durable messages and events out (ADR-0020).

One poll is one bounded conversation per account:

```text
MailSource.fetch(request)                 request: mode, window, size limit, cursor
        │
        ▼
raw .eml into the content-addressed store ┤ oversize → header-only, no raw object
        │
        ▼
parse → fingerprinted MailMessage + location
        │
        ▼
one transaction: messages + locations + attachments + cursor
        │
        ▼
repair the MailMessage → InboundEvent bridge (bounded, every round)
```

Three properties are the point of the design:

- **the cursor only advances over durably handled mail.** A batch is one transaction, and the
  cursor lands on the highest UID *this batch actually stored* — not on the server's maximum.
  A crash therefore replays work instead of skipping it;
- **a mailbox rebuild is reconciled, not continued.** When UIDVALIDITY changes, every stored UID
  is meaningless, so a bounded window is re-read and matched against stored fingerprints;
- **the bridge is repaired every round.** A message committed without its event, or an event
  committed without its link, is finished on the next sync rather than waiting for new mail.

Remote operational failures (DNS, TLS, authentication, protocol) become a per-account status, so
one broken account cannot stop the others or the rest of the daemon. Infrastructure failures —
the runtime database, the raw store's invariants, cancellation — propagate.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from assistant.application.event_inbox import EventInbox, IngestDisposition, IngestEvent
from assistant.domain.config import MailAccountConfig, MailConfig
from assistant.domain.errors import (
    MailAuthenticationError,
    MailConnectionError,
    MailCredentialsMissing,
    MailProtocolError,
)
from assistant.domain.mail import (
    MailAttachmentMetadata,
    MailBodyStatus,
    MailboxSyncMode,
    MailboxSyncState,
    MailMessage,
    MailMessageLocation,
    ReconciliationDecision,
    ReconciliationEvidence,
    choose_reconciliation_match,
    mail_content_fingerprint,
    new_mail_message_id,
)
from assistant.ports.clock import Clock
from assistant.ports.interval_waiter import IntervalWaiter
from assistant.ports.mail_parser import MailParser
from assistant.ports.mail_repository import FetchedMail, MailRepository
from assistant.ports.mail_source import (
    MailFetchMode,
    MailFetchRequest,
    MailSource,
    RemoteMailMessage,
)

LOGGER = logging.getLogger("assistant.mail")

MAIL_EVENT_TYPE = "mail.message.received"
"""The `InboundEvent` type every synchronized message is bridged as."""

BRIDGE_REPAIR_LIMIT = 100
"""How many unlinked messages one round may repair, so recovery stays bounded."""


class AccountSyncStatus(StrEnum):
    """The outcome of syncing one account."""

    SYNCED = "synced"
    OFFLINE = "offline"
    AUTH_ERROR = "auth_error"
    PROTOCOL_ERROR = "protocol_error"
    CREDENTIALS_MISSING = "credentials_missing"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class AccountSyncResult:
    """What one account's sync did, in counts a user can read."""

    account_id: str
    mailbox: str
    status: AccountSyncStatus
    uidvalidity: int | None = None
    fetched: int = 0
    new_messages: int = 0
    matched_existing: int = 0
    existing_locations: int = 0
    events_created: int = 0
    events_repaired: int = 0
    oversize: int = 0
    reconciled: bool = False
    reconciliation_conflicts: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class MailSyncResult:
    """The outcome of one sync round, per account."""

    accounts: tuple[AccountSyncResult, ...] = ()

    @property
    def synced(self) -> tuple[AccountSyncResult, ...]:
        """The accounts whose round completed."""
        return tuple(
            account for account in self.accounts if account.status is AccountSyncStatus.SYNCED
        )

    @property
    def events_created(self) -> int:
        """How many `InboundEvent`s were created across all accounts."""
        return sum(account.events_created for account in self.accounts)


@dataclass(frozen=True, slots=True)
class _MessageOutcome:
    """One stored message plus what happened to it."""

    fetched: FetchedMail
    decision: ReconciliationDecision = field(default_factory=ReconciliationDecision)


class MailSyncService:
    """Synchronizes configured IMAP accounts into durable mail state.

    The service is deliberately separate from the interpreter, the planner and the scheduler:
    Phase 5A receives mail. It does not classify, answer, draft or send anything.
    """

    name = "mail-sync"

    def __init__(
        self,
        config: MailConfig,
        repository: MailRepository,
        inbox: EventInbox,
        raw_store: object,
        clock: Clock,
        source_factory: Callable[[MailAccountConfig], MailSource],
        parser: MailParser,
        *,
        waiter: IntervalWaiter,
        bridge_repair_limit: int = BRIDGE_REPAIR_LIMIT,
    ) -> None:
        self._config = config
        self._repository = repository
        self._inbox = inbox
        self._raw_store = raw_store
        self._clock = clock
        self._source_factory = source_factory
        self._parser = parser
        self._waiter = waiter
        self._bridge_repair_limit = bridge_repair_limit

    async def sync_once(self, account_id: str | None = None) -> MailSyncResult:
        """Sync one account, or every enabled account, then repair the event bridge."""
        accounts = self._accounts(account_id)
        results: list[AccountSyncResult] = []
        for account in accounts:
            results.append(await self._sync_account(account))
        if accounts:
            created, repaired = await self.repair_bridge()
            if created or repaired:
                results = [
                    _with_bridge_counts(result, created=created, repaired=repaired)
                    for result in results
                    if result.status is AccountSyncStatus.SYNCED
                ] + [result for result in results if result.status is not AccountSyncStatus.SYNCED]
        return MailSyncResult(accounts=tuple(results))

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Sync on startup, then once per poll interval until asked to stop."""
        while not stop_event.is_set():
            try:
                result = await self.sync_once()
            except asyncio.CancelledError:
                raise
            _log_summary(result)
            await self._waiter.wait(self._config.poll_interval_seconds, stop_event)

    async def repair_bridge(self) -> tuple[int, int]:
        """Bridge stored messages that have no `InboundEvent` link yet.

        Returns `(events_created, events_repaired)`. Both crash windows are covered: a message
        stored without its event produces a new event here, and an event stored without its link
        is recognised as a duplicate and simply linked.
        """
        unlinked = await self._repository.list_unlinked_messages(
            limit=self._bridge_repair_limit
        )
        created = repaired = 0
        for message in unlinked:
            result = await self._inbox.ingest(
                IngestEvent(
                    source=f"mail:{message.account_id}",
                    event_type=MAIL_EVENT_TYPE,
                    external_id=f"message:{message.id}",
                    content=json.dumps(
                        {"account_id": message.account_id, "mail_message_id": str(message.id)},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            )
            await self._repository.link_inbound_event(
                mail_message_id=message.id,
                inbound_event_id=result.event.id,
                linked_at=self._clock.now(),
            )
            if result.disposition is IngestDisposition.CREATED:
                created += 1
            else:
                repaired += 1
        return created, repaired

    # ----------------------------------------------------------------- per account

    def _accounts(self, account_id: str | None) -> tuple[MailAccountConfig, ...]:
        if account_id is None:
            return self._config.enabled_accounts
        account = self._config.find(account_id)
        if account is None:
            raise MailProtocolError(f"no configured mail account {account_id!r}")
        return (account,)

    async def _sync_account(self, account: MailAccountConfig) -> AccountSyncResult:
        if not account.enabled:
            return AccountSyncResult(
                account_id=account.id,
                mailbox=account.mailbox,
                status=AccountSyncStatus.DISABLED,
            )
        try:
            source = self._source_factory(account)
        except MailCredentialsMissing as exc:
            return AccountSyncResult(
                account_id=account.id,
                mailbox=account.mailbox,
                status=AccountSyncStatus.CREDENTIALS_MISSING,
                error=str(exc),
            )
        state = await self._repository.get_sync_state(account.id, account.mailbox)
        mode = MailFetchMode.INITIAL if state is None else MailFetchMode.INCREMENTAL
        try:
            batch = await source.fetch(self._request(account, mode, state))
            reconciled = False
            if batch.uidvalidity_changed:
                reconciled = True
                batch = await source.fetch(
                    self._request(account, MailFetchMode.RECONCILIATION, state)
                )
        except MailAuthenticationError as exc:
            return _failed(account, AccountSyncStatus.AUTH_ERROR, exc)
        except MailConnectionError as exc:
            return _failed(account, AccountSyncStatus.OFFLINE, exc)
        except MailProtocolError as exc:
            return _failed(account, AccountSyncStatus.PROTOCOL_ERROR, exc)

        at = self._clock.now()
        fetched: list[FetchedMail] = []
        conflicts = 0
        for remote in batch.messages:
            outcome = await self._process(
                account,
                remote,
                uidvalidity=batch.state.uidvalidity,
                reconciled=reconciled,
                at=at,
            )
            if outcome.decision.conflicted:
                conflicts += 1
            fetched.append(outcome.fetched)
        cursor = _cursor_for(mode, state, fetched)
        applied = await self._repository.apply_fetched_batch(
            account_id=account.id,
            mailbox_name=account.mailbox,
            uidvalidity=batch.state.uidvalidity,
            last_seen_uid=cursor,
            messages=fetched,
            mode=MailboxSyncMode.RECONCILING if reconciled else MailboxSyncMode.NORMAL,
            at=at,
            reconciled=reconciled,
        )
        return AccountSyncResult(
            account_id=account.id,
            mailbox=account.mailbox,
            status=AccountSyncStatus.SYNCED,
            uidvalidity=batch.state.uidvalidity,
            fetched=len(batch.messages),
            new_messages=applied.created_messages,
            matched_existing=applied.matched_messages,
            existing_locations=applied.existing_locations,
            oversize=batch.oversize,
            reconciled=reconciled,
            reconciliation_conflicts=conflicts,
        )

    def _request(
        self,
        account: MailAccountConfig,
        mode: MailFetchMode,
        state: MailboxSyncState | None,
    ) -> MailFetchRequest:
        if mode is MailFetchMode.INITIAL:
            window = self._config.initial_fetch_limit
        elif mode is MailFetchMode.RECONCILIATION:
            window = self._config.reconciliation_window
        else:
            window = self._config.max_messages_per_poll
        incremental = mode is MailFetchMode.INCREMENTAL
        return MailFetchRequest(
            mailbox_name=account.mailbox,
            mode=mode,
            window=window,
            max_message_bytes=self._config.max_message_bytes,
            # A cursor and an expected UIDVALIDITY only make sense for an incremental fetch.
            # A reconciliation happens *because* the old values are meaningless, so it must not
            # hand them back to the server as an expectation.
            after_uid=state.last_seen_uid if incremental and state is not None else None,
            expected_uidvalidity=(
                state.uidvalidity if incremental and state is not None else None
            ),
        )

    async def _process(
        self,
        account: MailAccountConfig,
        remote: RemoteMailMessage,
        *,
        uidvalidity: int,
        reconciled: bool,
        at: datetime,
    ) -> _MessageOutcome:
        """Turn one fetched message into durable state, matching before storing when asked."""
        raw = remote.raw or b""
        parsed = (
            self._parser.parse_header_only(raw)
            if remote.header_only
            else self._parser.parse_full(raw)
        )
        body_status = (
            MailBodyStatus.OVERSIZE if remote.header_only else MailBodyStatus.AVAILABLE
        )
        raw_sha256: str | None = None
        raw_key: str | None = None
        if not remote.header_only:
            # The raw object is written before the database transaction: a failed transaction
            # leaves an unreferenced object, which is harmless, whereas the reverse would leave
            # a message claiming a body that does not exist.
            stored = await self._raw_store.store(raw)  # type: ignore[attr-defined]
            raw_sha256 = stored.sha256
            raw_key = stored.storage_key
        fingerprint = mail_content_fingerprint(
            message_id_header=parsed.message_id_header,
            from_address=parsed.from_address,
            date_header=parsed.date_header,
            subject=parsed.subject,
            body_text=parsed.body_text,
            attachment_sha256s=tuple(item.sha256 for item in parsed.attachments),
            size_bytes=remote.size_bytes,
        )
        decision = ReconciliationDecision()
        if reconciled:
            candidates = await self._repository.find_reconciliation_candidates(
                account_id=account.id,
                message_id_header=parsed.message_id_header,
                content_fingerprint=fingerprint,
                raw_sha256=raw_sha256,
            )
            decision = choose_reconciliation_match(
                ReconciliationEvidence(
                    content_fingerprint=fingerprint,
                    size_bytes=remote.size_bytes,
                    raw_sha256=raw_sha256,
                    message_id_header=parsed.message_id_header,
                    from_address=parsed.from_address,
                    date_header=parsed.date_header,
                ),
                candidates,
            )
        message_id = decision.matched_message_id
        message = MailMessage(
            id=message_id if message_id is not None else new_mail_message_id(),
            account_id=account.id,
            message_id_header=parsed.message_id_header,
            in_reply_to_header=parsed.in_reply_to_header,
            references=parsed.references,
            subject=parsed.subject,
            from_address=parsed.from_address,
            to_addresses=parsed.to_addresses,
            cc_addresses=parsed.cc_addresses,
            reply_to_addresses=parsed.reply_to_addresses,
            date_header=parsed.date_header,
            sent_at=parsed.sent_at,
            body_text=parsed.body_text,
            body_status=body_status,
            raw_sha256=raw_sha256,
            content_fingerprint=fingerprint,
            raw_storage_key=raw_key,
            size_bytes=remote.size_bytes,
            parse_warnings=parsed.warnings,
            first_seen_at=at,
            last_seen_at=at,
        )
        location = MailMessageLocation(
            message_id=message.id,
            account_id=account.id,
            mailbox_name=account.mailbox,
            uidvalidity=uidvalidity,
            uid=remote.uid,
            first_seen_at=at,
            last_seen_at=at,
        )
        attachments = tuple(
            MailAttachmentMetadata(
                message_id=message.id,
                ordinal=item.ordinal,
                filename=item.filename,
                content_type=item.content_type,
                content_disposition=item.content_disposition,
                size_bytes=item.size_bytes,
                sha256=item.sha256,
            )
            for item in parsed.attachments
        )
        return _MessageOutcome(
            fetched=FetchedMail(message=message, location=location, attachments=attachments),
            decision=decision,
        )

def _cursor_for(
    mode: MailFetchMode, state: MailboxSyncState | None, fetched: list[FetchedMail]
) -> int:
    """The cursor after this batch: the highest *stored* UID, never the server's maximum."""
    if fetched:
        return max(item.location.uid for item in fetched)
    if mode is MailFetchMode.INCREMENTAL and state is not None:
        return state.last_seen_uid
    return 0


def _failed(
    account: MailAccountConfig, status: AccountSyncStatus, error: Exception
) -> AccountSyncResult:
    return AccountSyncResult(
        account_id=account.id,
        mailbox=account.mailbox,
        status=status,
        error=str(error),
    )


def _with_bridge_counts(
    result: AccountSyncResult, *, created: int, repaired: int
) -> AccountSyncResult:
    return AccountSyncResult(
        account_id=result.account_id,
        mailbox=result.mailbox,
        status=result.status,
        uidvalidity=result.uidvalidity,
        fetched=result.fetched,
        new_messages=result.new_messages,
        matched_existing=result.matched_existing,
        existing_locations=result.existing_locations,
        events_created=created,
        events_repaired=repaired,
        oversize=result.oversize,
        reconciled=result.reconciled,
        reconciliation_conflicts=result.reconciliation_conflicts,
        error=result.error,
    )


def _log_summary(result: MailSyncResult) -> None:
    """One summary line per account, and never a single message's content."""
    for account in result.accounts:
        if account.status is AccountSyncStatus.SYNCED:
            LOGGER.info(
                "mail sync completed account=%s mailbox=%s fetched=%d new=%d matched=%d "
                "events=%d repaired=%d oversize=%d reconciled=%s",
                account.account_id,
                account.mailbox,
                account.fetched,
                account.new_messages,
                account.matched_existing,
                account.events_created,
                account.events_repaired,
                account.oversize,
                account.reconciled,
            )
        elif account.status is not AccountSyncStatus.DISABLED:
            LOGGER.warning(
                "mail sync failed account=%s status=%s error=%s",
                account.account_id,
                account.status.value,
                account.error,
            )


__all__ = [
    "BRIDGE_REPAIR_LIMIT",
    "MAIL_EVENT_TYPE",
    "AccountSyncResult",
    "AccountSyncStatus",
    "MailSyncResult",
    "MailSyncService",
]
