"""Preparing an exact, approvable mail send from one draft version (ADR-0024).

```text
pw mail send prepare DRAFT --case CASE
        │
        ├── the case must be OPEN                        (a closed case accepts no new work)
        ├── the draft must be send-ready                 (open questions acknowledged or absent)
        ├── the account must have TLS SMTP configuration (a credential is not needed yet)
        ├── recipients, From, subject, body, reply headers — all derived locally
        ├── one Message-ID and one Date header, minted now and frozen into the payload
        ▼
immutable ActionRequest("mail.send") + MailSendLink, in one transaction
```

The model is not consulted anywhere in this module, and nothing here talks to a server: preparing
a send produces a *question* ("may I send exactly this?"), and answering it is a separate human
step through the Phase 6A approval boundary.

The Message-ID is generated here rather than at send time for a specific reason: the same string
has to appear in the approved payload, in the transmitted bytes and in the Sent-folder lookup, and
a value regenerated per attempt would make "did this message arrive?" unanswerable.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from assistant.domain.action import ActionRequest, ActionType
from assistant.domain.case import Case, CaseId, CaseStatus
from assistant.domain.config import MailAccountConfig
from assistant.domain.errors import (
    CaseNotFound,
    CaseNotOpen,
    MailDraftNeedsUserInput,
    MailDraftNotFound,
    MailMessageNotFound,
    MailSendAlreadyPrepared,
    MailSendNotConfigured,
)
from assistant.domain.mail import MailMessage, normalize_message_id
from assistant.domain.mail_draft import MailDraft, MailDraftId
from assistant.domain.mail_send import MailSendLink, MailSendPayload
from assistant.ports.action_repository import ActionRepository
from assistant.ports.case_repository import CaseRepository
from assistant.ports.clock import Clock
from assistant.ports.mail_draft_repository import MailDraftRepository
from assistant.ports.mail_repository import MailRepository
from assistant.ports.mail_send_repository import MailSendRepository

LOGGER = logging.getLogger("assistant.mail")

MAIL_SEND_ACTION_TYPE = ActionType("mail.send")
"""The action type every prepared outbound delivery uses."""

MAX_SEND_REFERENCES = 50


@dataclass(frozen=True, slots=True)
class MailSendPreparation:
    """The prepared action, the exact payload it carries, and the draft it came from."""

    action: ActionRequest
    payload: MailSendPayload
    link: MailSendLink
    draft: MailDraft


class MailSendActionService:
    """Turns one reviewed draft version into one approvable send action."""

    def __init__(
        self,
        drafts: MailDraftRepository,
        mail: MailRepository,
        cases: CaseRepository,
        actions: ActionRepository,
        send_repository: MailSendRepository,
        clock: Clock,
        *,
        accounts: tuple[MailAccountConfig, ...] = (),
        message_id_factory: Callable[[str], str],
        date_header_factory: Callable[[datetime], str],
    ) -> None:
        self._drafts = drafts
        self._mail = mail
        self._cases = cases
        self._actions = actions
        self._send = send_repository
        self._clock = clock
        self._accounts = {account.id: account for account in accounts}
        self._message_id_factory = message_id_factory
        self._date_header_factory = date_header_factory

    async def prepare_send(
        self,
        draft_id: MailDraftId | str,
        case_id: CaseId | str,
    ) -> MailSendPreparation:
        """Prepare one exact send action for one draft version inside one open case.

        Raises:
            MailDraftNotFound: no such draft.
            CaseNotFound: no such case.
            CaseNotOpen: the case is already terminal.
            MailDraftNeedsUserInput: the draft's open questions have not been acknowledged.
            MailSendNotConfigured: the draft's account has no usable outbound configuration.
            MailSendAlreadyPrepared: this exact draft version already has a send action.
            MailMessageNotFound: the reply target is gone.
        """
        draft = await self._require_draft(draft_id)
        case = await self._require_open_case(case_id)
        if draft.send_blocked_by_user_input:
            raise MailDraftNeedsUserInput(draft.id)
        account = self._require_smtp_account(draft.account_id)
        existing = await self._send.get_link_for_draft_version(draft.id, draft.version)
        if existing is not None:
            raise MailSendAlreadyPrepared(draft.id, draft.version, existing.action_id)
        reply_to, references = await self._reply_headers(draft)
        now = self._clock.now()
        from_address = account.from_address or ""
        domain = from_address.partition("@")[2]
        payload = MailSendPayload(
            draft_id=draft.id,
            draft_version=draft.version,
            account_id=draft.account_id,
            from_address=from_address,
            to_addresses=draft.to_addresses,
            subject=draft.subject,
            body_text=draft.body_text,
            rfc_message_id=self._message_id_factory(domain),
            date_header=self._date_header_factory(now),
            in_reply_to_header=reply_to,
            references=references,
        )
        action = ActionRequest.prepare(
            case_id=case.id,
            action_type=MAIL_SEND_ACTION_TYPE,
            payload=payload.to_payload(),
            at=now,
        )
        link = MailSendLink(
            action_id=action.id,
            draft_id=draft.id,
            draft_version=draft.version,
            rfc_message_id=payload.rfc_message_id,
            created_at=now,
        )
        stored = await self._send.add_action_with_link(action, link)
        LOGGER.info(
            "mail send prepared account=%s draft_version=%d",
            draft.account_id,
            draft.version,
        )
        return MailSendPreparation(
            action=stored, payload=payload, link=link, draft=draft
        )

    # ------------------------------------------------------------------ internals

    async def _require_draft(self, draft_id: MailDraftId | str) -> MailDraft:
        resolved = (
            draft_id
            if isinstance(draft_id, UUID)
            else await self._drafts.resolve_draft_id(draft_id)
        )
        draft = await self._drafts.get_draft(resolved)
        if draft is None:  # pragma: no cover - resolution just found it
            raise MailDraftNotFound(draft_id)
        return draft

    async def _require_open_case(self, case_id: CaseId | str) -> Case:
        resolved = (
            case_id
            if isinstance(case_id, UUID)
            else await self._cases.resolve_case_id(case_id)
        )
        case = await self._cases.get_case(resolved)
        if case is None:
            raise CaseNotFound(case_id)
        if case.status is not CaseStatus.OPEN:
            raise CaseNotOpen(case.id, case.status.value)
        return case

    def _require_smtp_account(self, account_id: str) -> MailAccountConfig:
        account = self._accounts.get(account_id)
        if account is None:
            raise MailSendNotConfigured(
                account_id, "this host configures no such mail account"
            )
        if not account.smtp_configured:
            raise MailSendNotConfigured(
                account_id,
                "the account has no smtp_host/smtp_username/from_address block, so it is "
                "receive-only",
            )
        return account

    async def _reply_headers(
        self, draft: MailDraft
    ) -> tuple[str | None, tuple[str, ...]]:
        """Derive `In-Reply-To` and `References` from the message the draft replies to."""
        original: MailMessage | None = await self._mail.get_message(
            draft.reply_to_message_id
        )
        if original is None:
            raise MailMessageNotFound(draft.reply_to_message_id)
        message_id = normalize_message_id(original.message_id_header)
        if message_id is None:
            # The original carried no usable Message-ID: a reply without threading headers is
            # still a reply, and inventing one would be worse than omitting it.
            return None, ()
        references: list[str] = []
        for value in original.references:
            normalized = normalize_message_id(value)
            if normalized is not None and normalized not in references:
                references.append(normalized)
        if message_id not in references:
            references.append(message_id)
        return message_id, tuple(references[:MAX_SEND_REFERENCES])

__all__ = [
    "MAIL_SEND_ACTION_TYPE",
    "MAX_SEND_REFERENCES",
    "MailSendActionService",
    "MailSendPreparation",
]
