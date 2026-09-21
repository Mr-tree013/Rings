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
from assistant.domain.new_mail_draft import NewMailDraftId

MAIL_SEND_SCHEMA_VERSION = 1
"""Bump when the payload shape changes in a way an executor must notice.

Phase 10E added one *closed discriminator* (`kind`) without changing a single field SMTP reads:
the transport's behaviour is identical for a new letter and for a reply, because reply headers were
already optional and the executor simply omits what is absent. Historical payloads — written
before the discriminator existed — therefore stay valid, parse as `reply`, and keep the version
they were minted with. A change that *did* alter what leaves the machine would still be a bump.
"""

MAX_SEND_BODY_CHARS = 200_000
MAX_SEND_SUBJECT_CHARS = 2000
MAX_SEND_HEADER_CHARS = 2000
MAX_SEND_REFERENCES = 50

_MESSAGE_ID_PATTERN = re.compile(r"^<[!-~]+@[!-~]+>$")


def new_mail_send_reconciliation_id() -> UUID:
    """Generate a fresh reconciliation identity."""
    return uuid4()


class MailSendKind(StrEnum):
    """Which draft a prepared send was snapshotted from (ADR-0037 §7)."""

    REPLY = "reply"
    """A reply to a stored message. It may carry `In-Reply-To` and `References`."""

    NEW = "new"
    """A new letter. It never carries reply headers: there is nothing to reply to."""


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
    kind: MailSendKind = MailSendKind.REPLY
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
        if self.kind is MailSendKind.NEW and (
            self.in_reply_to_header is not None or self.references
        ):
            # A new letter that claims to answer something is a lie in the headers, and the
            # preview would show it. The closed variant is enforced here, not by convention.
            raise InvalidMailSend("a new mail send must not carry reply headers")

    def to_payload(self) -> dict[str, object]:
        """The canonical JSON document stored in the approved `ActionRequest`."""
        return {
            "schema_version": self.schema_version,
            "kind": self.kind.value,
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
        legacy = expected
        expected = expected | {"kind"}
        # A payload written before the discriminator existed is a reply payload; anything else —
        # a missing field, or a key this build does not know — is refused.
        if set(payload) != expected and set(payload) != legacy:
            missing = sorted(expected - set(payload))
            extra = sorted(set(payload) - expected)
            raise InvalidMailSend(
                f"a mail send payload has the wrong fields "
                f"(missing {missing}, extra {extra})"
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
        raw_kind = payload.get("kind")
        if raw_kind is None:
            kind = MailSendKind.REPLY
        elif isinstance(raw_kind, str):
            try:
                kind = MailSendKind(raw_kind)
            except ValueError as exc:
                raise InvalidMailSend(f"unknown mail send kind {raw_kind!r}") from exc
        else:
            raise InvalidMailSend("kind must be a string")
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
            kind=kind,
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
    """The binding between one prepared send action and the draft version it snapshotted.

    Exactly one of the two draft references is set: a reply draft keeps its source-message
    invariant, and a new-mail draft keeps its own table. The link is what makes "one draft version,
    one action" and "one Message-ID, one action" database facts for both kinds, which is why the
    two converge here rather than in two parallel pipelines.
    """

    action_id: UUID
    draft_version: int
    rfc_message_id: str
    created_at: datetime
    draft_id: MailDraftId | None = None
    new_draft_id: NewMailDraftId | None = None

    def __post_init__(self) -> None:
        if (self.draft_id is None) == (self.new_draft_id is None):
            raise InvalidMailSend(
                "a send link names exactly one draft: a reply draft or a new-mail draft"
            )
        if self.draft_version < 1:
            raise InvalidMailSend("a send link needs a positive draft version")
        object.__setattr__(
            self, "rfc_message_id", validate_rfc_message_id(self.rfc_message_id)
        )
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise InvalidMailSend("created_at must be timezone-aware")

    @property
    def kind(self) -> MailSendKind:
        """Which draft this action was snapshotted from."""
        return MailSendKind.NEW if self.new_draft_id is not None else MailSendKind.REPLY

    @property
    def reply_draft_id(self) -> MailDraftId | None:
        """The reply draft this action came from, or `None` for new mail."""
        return self.draft_id

    @classmethod
    def for_reply(
        cls,
        *,
        action_id: UUID,
        draft_id: MailDraftId,
        draft_version: int,
        rfc_message_id: str,
        created_at: datetime,
    ) -> MailSendLink:
        """One link to a reply draft version."""
        return cls(
            action_id=action_id,
            draft_id=draft_id,
            draft_version=draft_version,
            rfc_message_id=rfc_message_id,
            created_at=created_at,
        )

    @classmethod
    def for_new_mail(
        cls,
        *,
        action_id: UUID,
        new_draft_id: NewMailDraftId,
        draft_version: int,
        rfc_message_id: str,
        created_at: datetime,
    ) -> MailSendLink:
        """One link to a new-mail draft version."""
        return cls(
            action_id=action_id,
            new_draft_id=new_draft_id,
            draft_version=draft_version,
            rfc_message_id=rfc_message_id,
            created_at=created_at,
        )


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
    "MailSendKind",
    "MailSendLink",
    "MailSendPayload",
    "MailSendReconciliation",
    "MailSendReconciliationResult",
    "new_mail_send_reconciliation_id",
    "validate_rfc_message_id",
]
