"""Approved mail delivery: the exact payload, its link and its reconciliation (ADR-0024).

Sending is the first capability with a real external effect, so the values here are strict about
one thing: **what was approved is what leaves the machine**. `MailSendPayload` is frozen, is
carried inside an immutable `ActionRequest`, and every field in it is derived locally — the
recipients, the From, the subject, the body, the Message-ID and the reply headers. The model is
not consulted at send time at all.

The RFC Message-ID deserves its own note: it is minted *before* approval, stored in the payload,
and is therefore the same string in the fingerprint, in the transmitted bytes and in the
Sent-folder lookup. It is never regenerated on a retry or a reconciliation, because a second id
would make "did this exact message arrive?" unanswerable.

Nothing here knows how to reach a server. Transport configuration — host, port, security,
username — stays in the account configuration, and the password stays in the environment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from assistant.domain.errors import InvalidMailSend
from assistant.domain.mail import MAX_ADDRESSES, MailAccountId, validate_account_id
from assistant.domain.mail_draft import MailDraftId, mailbox_address

MAIL_SEND_SCHEMA_VERSION = 1
"""Bump when the payload shape changes in a way an executor must notice."""

MAX_SEND_BODY_CHARS = 200_000
MAX_SEND_SUBJECT_CHARS = 2000
MAX_SEND_HEADER_CHARS = 2000
MAX_SEND_REFERENCES = 50

_MESSAGE_ID_PATTERN = re.compile(r"^<[!-~]+@[!-~]+>$")


def new_mail_send_reconciliation_id() -> UUID:
    """Generate a fresh reconciliation identity."""
    return uuid4()


def validate_rfc_message_id(value: str) -> str:
    """Return a usable RFC 5322 `msg-id`, or raise.

    Only printable, whitespace-free characters are accepted: the value is interpolated into an
    IMAP `HEADER` search and into the message header block, so a carriage return or a quote in it
    would be an injection, not a formatting quirk.
    """
    candidate = value.strip()
    if not candidate:
        raise InvalidMailSend("a mail send needs an RFC Message-ID")
    if len(candidate) > MAX_SEND_HEADER_CHARS:
        raise InvalidMailSend("an RFC Message-ID must be at most 2000 characters")
    if not _MESSAGE_ID_PATTERN.match(candidate):
        raise InvalidMailSend(
            f"an RFC Message-ID must look like <local@domain> (got {value!r})"
        )
    return candidate


@dataclass(frozen=True, slots=True)
class MailSendPayload:
    """Everything one approved mail send contains. Immutable, and complete."""

    draft_id: MailDraftId
    draft_version: int
    account_id: MailAccountId
    from_address: str
    to_addresses: tuple[str, ...]
    subject: str
    body_text: str
    rfc_message_id: str
    date_header: str
    schema_version: int = MAIL_SEND_SCHEMA_VERSION
    in_reply_to_header: str | None = None
    references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_account_id(self.account_id)
        if self.schema_version != MAIL_SEND_SCHEMA_VERSION:
            raise InvalidMailSend(
                f"unsupported mail send schema version {self.schema_version}"
            )
        if self.draft_version < 1:
            raise InvalidMailSend("a mail send needs a positive draft version")
        sender = mailbox_address(self.from_address)
        if sender is None or sender != self.from_address:
            raise InvalidMailSend(
                f"a mail send needs a plain from address, not {self.from_address!r}"
            )
        if not self.to_addresses:
            raise InvalidMailSend("a mail send needs at least one recipient")
        if len(self.to_addresses) > MAX_ADDRESSES:
            raise InvalidMailSend(
                f"a mail send may have at most {MAX_ADDRESSES} recipients"
            )
        for address in self.to_addresses:
            if mailbox_address(address) != address:
                raise InvalidMailSend(f"unusable recipient address {address!r}")
        subject = " ".join(self.subject.split())
        if not subject:
            raise InvalidMailSend("a mail send needs a subject")
        if len(subject) > MAX_SEND_SUBJECT_CHARS:
            raise InvalidMailSend("a mail send subject must be at most 2000 characters")
        object.__setattr__(self, "subject", subject)
        body = self.body_text.strip()
        if not body:
            raise InvalidMailSend("a mail send needs a body")
        if len(body) > MAX_SEND_BODY_CHARS:
            raise InvalidMailSend("a mail send body must be at most 200000 characters")
        object.__setattr__(self, "body_text", body)
        object.__setattr__(self, "rfc_message_id", validate_rfc_message_id(self.rfc_message_id))
        if not self.date_header.strip():
            raise InvalidMailSend("a mail send needs a Date header")
        if len(self.references) > MAX_SEND_REFERENCES:
            raise InvalidMailSend("a mail send may carry at most 50 references")
        for value in self.references:
            if not value.strip() or len(value) > MAX_SEND_HEADER_CHARS:
                raise InvalidMailSend("every reference must be a short non-blank id")
        if self.in_reply_to_header is not None:
            reply_to = self.in_reply_to_header.strip()
            if not reply_to or len(reply_to) > MAX_SEND_HEADER_CHARS:
                raise InvalidMailSend("an In-Reply-To header must be a short non-blank id")
            object.__setattr__(self, "in_reply_to_header", reply_to)

    def to_payload(self) -> dict[str, object]:
        """The canonical JSON document stored in the approved `ActionRequest`."""
        return {
            "schema_version": self.schema_version,
            "draft_id": str(self.draft_id),
            "draft_version": self.draft_version,
            "account_id": self.account_id,
            "from_address": self.from_address,
            "to_addresses": list(self.to_addresses),
            "subject": self.subject,
            "body_text": self.body_text,
            "rfc_message_id": self.rfc_message_id,
            "date_header": self.date_header,
            "in_reply_to_header": self.in_reply_to_header,
            "references": list(self.references),
        }

    @classmethod
    def from_payload(cls, payload: object) -> MailSendPayload:
        """Rebuild the payload from an approved action, refusing anything unexpected.

        This is the executor's and the reconciler's entry point, so it is strict: a missing field,
        a wrong type or an unknown schema version is an error rather than a default.

        Raises:
            InvalidMailSend: the payload is not a usable mail send.
        """
        if not isinstance(payload, dict):
            raise InvalidMailSend("a mail send payload must be a JSON object")
        expected = {
            "schema_version",
            "draft_id",
            "draft_version",
            "account_id",
            "from_address",
            "to_addresses",
            "subject",
            "body_text",
            "rfc_message_id",
            "date_header",
            "in_reply_to_header",
            "references",
        }
        if set(payload) != expected:
            missing = sorted(expected - set(payload))
            extra = sorted(set(payload) - expected)
            raise InvalidMailSend(
                f"a mail send payload has the wrong fields (missing {missing}, extra {extra})"
            )
        version = payload["schema_version"]
        if not isinstance(version, int) or isinstance(version, bool):
            raise InvalidMailSend("schema_version must be an integer")
        if version != MAIL_SEND_SCHEMA_VERSION:
            raise InvalidMailSend(f"unsupported mail send schema version {version}")
        draft_version = payload["draft_version"]
        if not isinstance(draft_version, int) or isinstance(draft_version, bool):
            raise InvalidMailSend("draft_version must be an integer")
        recipients = payload["to_addresses"]
        if not isinstance(recipients, list):
            raise InvalidMailSend("to_addresses must be a list")
        references = payload["references"]
        if not isinstance(references, list):
            raise InvalidMailSend("references must be a list")
        reply_to = payload["in_reply_to_header"]
        if reply_to is not None and not isinstance(reply_to, str):
            raise InvalidMailSend("in_reply_to_header must be a string or null")
        return cls(
            draft_id=UUID(_text(payload, "draft_id")),
            draft_version=draft_version,
            account_id=_text(payload, "account_id"),
            from_address=_text(payload, "from_address"),
            to_addresses=tuple(str(item) for item in recipients),
            subject=_text(payload, "subject"),
            body_text=_text(payload, "body_text"),
            rfc_message_id=_text(payload, "rfc_message_id"),
            date_header=_text(payload, "date_header"),
            in_reply_to_header=reply_to,
            references=tuple(str(item) for item in references),
        )


def _text(payload: dict[str, object], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str):
        raise InvalidMailSend(f"{key} must be a string")
    return value


@dataclass(frozen=True, slots=True)
class MailSendLink:
    """The binding between one approved send action and the draft version it snapshotted."""

    action_id: UUID
    draft_id: MailDraftId
    draft_version: int
    rfc_message_id: str
    created_at: datetime

    def __post_init__(self) -> None:
        if self.draft_version < 1:
            raise InvalidMailSend("a send link needs a positive draft version")
        object.__setattr__(
            self, "rfc_message_id", validate_rfc_message_id(self.rfc_message_id)
        )
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise InvalidMailSend("created_at must be timezone-aware")


class MailSendReconciliationResult(StrEnum):
    """What a look in the Sent mailbox concluded.

    Only `FOUND` proves anything. `NOT_FOUND` means the lookup did not see the message — which is
    not the same as the message not existing, and never a reason to send again.
    """

    FOUND = "found"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class MailSendReconciliation:
    """One recorded Sent-folder lookup, kept forever."""

    action_id: UUID
    execution_run_id: UUID
    result: MailSendReconciliationResult
    checked_at: datetime
    id: UUID = field(default_factory=new_mail_send_reconciliation_id)
    mailbox_name: str | None = None
    uidvalidity: int | None = None
    uid: int | None = None

    def __post_init__(self) -> None:
        if self.checked_at.tzinfo is None or self.checked_at.utcoffset() is None:
            raise InvalidMailSend("checked_at must be timezone-aware")
        found = self.result is MailSendReconciliationResult.FOUND
        located = self.mailbox_name is not None and self.uid is not None
        if found != located:
            raise InvalidMailSend(
                "only a FOUND reconciliation names a mailbox and a UID"
            )
        if self.uid is not None and self.uid < 1:
            raise InvalidMailSend("uid must be a positive integer")
        if self.uidvalidity is not None and self.uidvalidity < 1:
            raise InvalidMailSend("uidvalidity must be a positive integer")


__all__ = [
    "MAIL_SEND_SCHEMA_VERSION",
    "MAX_SEND_BODY_CHARS",
    "MailSendLink",
    "MailSendPayload",
    "MailSendReconciliation",
    "MailSendReconciliationResult",
    "new_mail_send_reconciliation_id",
    "validate_rfc_message_id",
]
