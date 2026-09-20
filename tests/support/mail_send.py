"""Helpers for the approved-send tests: a configured account, a scripted SMTP server, a Sent box.

Neither SMTP nor IMAP is ever contacted: the executor and the lookup are driven through strict
fakes that record the exact call sequence, so the tests can assert *where* a failure happened —
which is what decides whether a result is a definite FAILED or an ambiguous UNKNOWN.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from assistant.domain.config import (
    AssistantConfig,
    MailAccountConfig,
    MailConfig,
)
from assistant.domain.mail import MailMessage
from assistant.domain.mail_draft import MailDraft, MailDraftOrigin
from assistant.ports.sent_mail_lookup import (
    SentMailLookupOutcome,
    SentMailLookupResult,
    SentMailMatch,
)
from tests.support.mail_drafts import NOW

SEND_ACCOUNT_ID = "smail"
FROM_ADDRESS = "student@example.edu"
TO_ADDRESS = "ada@example.edu"
SENT_MAILBOX = "Sent"
SMTP_PASSWORD = "SMTP-SECRET-PASSWORD"


def smtp_account(**overrides: object) -> MailAccountConfig:
    """One account whose inbound and outbound halves are both configured."""
    values: dict[str, object] = {
        "id": SEND_ACCOUNT_ID,
        "host": "imap.example.edu",
        "username": "student@example.edu",
        "mailbox": "INBOX",
        "smtp_host": "smtp.example.edu",
        "smtp_username": "student@example.edu",
        "from_address": FROM_ADDRESS,
        "sent_mailbox": SENT_MAILBOX,
    }
    values.update(overrides)
    return MailAccountConfig(**values)  # type: ignore[arg-type]


def assistant_config(accounts: tuple[MailAccountConfig, ...] | None = None) -> AssistantConfig:
    """A host configuration with one (or the given) mail accounts."""
    return AssistantConfig(mail=MailConfig(accounts=accounts or (smtp_account(),)))


def build_sendable_draft(
    *,
    draft_id: UUID | None = None,
    version: int = 1,
    needs_user_input: tuple[str, ...] = (),
    acknowledged_at: datetime | None = None,
    body_text: str = "Dear Ada, I will submit the report on Friday. Best regards.",
    subject: str = "Re: SE lab deadline",
    account_id: str = SEND_ACCOUNT_ID,
    to_addresses: tuple[str, ...] = (TO_ADDRESS,),
) -> MailDraft:
    """A stored-shape draft, ready (or not) to be prepared for sending."""
    return MailDraft(
        id=uuid4() if draft_id is None else draft_id,
        account_id=account_id,
        reply_to_message_id=uuid4(),
        to_addresses=to_addresses,
        subject=subject,
        body_text=body_text,
        needs_user_input=needs_user_input,
        needs_user_input_acknowledged_at=acknowledged_at,
        origin=MailDraftOrigin.MODEL_GENERATED,
        version=version,
        generation_input_fingerprint="a" * 64,
        created_at=NOW,
        updated_at=NOW,
    )


@dataclass
class StrictSmtpServer:
    """A scripted `smtplib` surface that records the exact conversation.

    `fail_at` selects the stage that raises, which is exactly what distinguishes a definite
    failure (nothing was handed over) from an ambiguous one (the data may have been accepted).
    """

    fail_at: str | None = None
    error: BaseException | None = None
    security: str = "starttls"
    calls: list[tuple[str, object]] = field(default_factory=list)
    contexts: list[ssl.SSLContext] = field(default_factory=list)

    def _maybe_fail(self, stage: str) -> None:
        if self.fail_at == stage:
            raise self.error if self.error is not None else RuntimeError(f"failed at {stage}")

    def ehlo(self) -> tuple[int, bytes]:
        self.calls.append(("ehlo", None))
        self._maybe_fail("ehlo")
        return 250, b"ok"

    def starttls(self, *, context: ssl.SSLContext) -> tuple[int, bytes]:
        self.calls.append(("starttls", None))
        self.contexts.append(context)
        self._maybe_fail("starttls")
        return 220, b"ready"

    def login(self, user: str, password: str) -> tuple[int, bytes]:
        self.calls.append(("login", (user, password)))
        self._maybe_fail("login")
        return 235, b"authenticated"

    def mail(self, sender: str) -> tuple[int, bytes]:
        self.calls.append(("mail", sender))
        self._maybe_fail("mail")
        return 250, b"ok"

    def rcpt(self, recipient: str) -> tuple[int, bytes]:
        self.calls.append(("rcpt", recipient))
        self._maybe_fail("rcpt")
        return 250, b"ok"

    def data(self, message: bytes | str) -> tuple[int, bytes]:
        payload = message.encode("utf-8") if isinstance(message, str) else message
        self.calls.append(("data", payload))
        self._maybe_fail("data")
        return 250, b"queued"

    def quit(self) -> tuple[int, bytes]:
        self.calls.append(("quit", None))
        return 221, b"bye"

    @property
    def stages(self) -> list[str]:
        """Just the stage names, in order."""
        return [name for name, _ in self.calls]

    @property
    def sent_bytes(self) -> bytes | None:
        """The exact bytes handed to `DATA`, when the conversation got that far."""
        for name, value in self.calls:
            if name == "data" and isinstance(value, bytes):
                return value
        return None


@dataclass
class FakeSentLookup:
    """A scripted Sent-mailbox lookup."""

    matches: tuple[SentMailMatch, ...] = ()
    outcome: SentMailLookupOutcome | None = None
    error: Exception | None = None
    answered: list[tuple[str, str]] = field(default_factory=list)

    def with_match(self, uid: int = 42, uidvalidity: int = 7) -> FakeSentLookup:
        """Answer FOUND with one match."""
        self.matches = (
            SentMailMatch(
                mailbox_name=SENT_MAILBOX, uidvalidity=uidvalidity, uid=uid
            ),
        )
        self.outcome = SentMailLookupOutcome.FOUND
        return self

    def with_ambiguity(self) -> FakeSentLookup:
        """Answer AMBIGUOUS with two matches."""
        self.matches = tuple(
            SentMailMatch(mailbox_name=SENT_MAILBOX, uidvalidity=7, uid=uid)
            for uid in (42, 43)
        )
        self.outcome = SentMailLookupOutcome.AMBIGUOUS
        return self

    def with_nothing(self) -> FakeSentLookup:
        """Answer NOT_FOUND."""
        self.matches = ()
        self.outcome = SentMailLookupOutcome.NOT_FOUND
        return self

    def with_error(self, error: Exception) -> FakeSentLookup:
        """Raise instead of answering."""
        self.error = error
        return self

    async def find_message(
        self, *, mailbox_name: str, rfc_message_id: str
    ) -> SentMailLookupResult:
        self.answered.append((mailbox_name, rfc_message_id))
        if self.error is not None:
            raise self.error
        outcome = self.outcome or SentMailLookupOutcome.NOT_FOUND
        return SentMailLookupResult(outcome=outcome, matches=self.matches)


def reply_target(
    *, message_id: str = "<original@example.edu>", references: tuple[str, ...] = ()
) -> MailMessage:
    """The incoming message a draft replies to, for reply-header derivation."""
    from tests.support.mail_intelligence import build_message

    return build_message(
        message_id_header=message_id,
        references=references,
        from_address="ada@example.edu",
        subject="SE lab deadline",
        at=datetime(2026, 9, 22, 9, 0, tzinfo=UTC),
    )


__all__ = [
    "FROM_ADDRESS",
    "SEND_ACCOUNT_ID",
    "SENT_MAILBOX",
    "SMTP_PASSWORD",
    "TO_ADDRESS",
    "FakeSentLookup",
    "StrictSmtpServer",
    "assistant_config",
    "build_sendable_draft",
    "reply_target",
    "smtp_account",
]
