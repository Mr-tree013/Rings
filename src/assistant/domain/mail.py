"""Mail domain: durable inbound messages, their locations and mailbox state (ADR-0020).

An IMAP UID means nothing on its own. It is only meaningful together with the account, the
mailbox and the `UIDVALIDITY` the server reported when it handed that UID out — a server that
rebuilds a mailbox may reuse UID 1 for a completely different message, and a cursor that
survived such a rebuild would silently skip or duplicate mail.

So identity is split in two:

- `MailMessage` is the logical message: a stable internal UUID that no IMAP detail determines,
  carrying the parsed headers, body text, attachment metadata and the fingerprints used to
  recognise the same message again after a mailbox was rebuilt.
- `MailMessageLocation` is where that message was seen: `(account, mailbox, uidvalidity, uid)`.

Nothing here is a promise of exactly-once delivery across arbitrary server rebuilds; the
fingerprints exist so a *bounded* reconciliation can reuse stable identities when the evidence
is convincing, and refuse to merge when it is not.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from assistant.domain.errors import (
    InvalidMailboxState,
    InvalidMailMessage,
)

MailMessageId = UUID
"""Stable identity of one logical mail message."""


def new_mail_message_id() -> MailMessageId:
    """Generate a fresh stable message identity."""
    return uuid4()

MailAccountId = str
"""Stable identity of one configured mail account (never an address or a host name)."""

ACCOUNT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

MAX_SUBJECT_CHARS = 2000
MAX_MESSAGE_ID_CHARS = 2000
MAX_ADDRESS_CHARS = 500
MAX_ADDRESSES = 50
MAX_REFERENCES = 50
MAX_FILENAME_CHARS = 255


def validate_account_id(account_id: str) -> str:
    """Return `account_id` when it is a valid stable identity, else raise."""
    if not ACCOUNT_ID_PATTERN.match(account_id):
        raise InvalidMailMessage(
            f"invalid mail account id {account_id!r}: expected {ACCOUNT_ID_PATTERN.pattern}"
        )
    return account_id


def normalize_header_value(value: str | None, *, limit: int) -> str | None:
    """Collapse whitespace, bound the length, and keep `None` for an absent header.

    Unfolding and whitespace collapsing happen first; an overlong value is truncated
    deterministically with an ellipsis marker instead of being allowed to fill the database.
    """
    if value is None:
        return None
    collapsed = " ".join(value.split())
    if not collapsed:
        return None
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "\u2026"


def normalize_message_id(value: str | None) -> str | None:
    """Normalise one Message-ID-like header value."""
    return normalize_header_value(value, limit=MAX_MESSAGE_ID_CHARS)


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidMailMessage(f"{field_name} must be timezone-aware")


def _require_aware_state(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidMailboxState(f"{field_name} must be timezone-aware")


class MailBodyStatus(StrEnum):
    """Whether the body of a message is available locally.

    An oversize message is still a durable message with a location, a cursor advancement and an
    event: only its body is missing, because the configured size limit said so.
    """

    AVAILABLE = "available"
    OVERSIZE = "oversize"


class MailboxSyncMode(StrEnum):
    """How the last completed sync treated this mailbox."""

    NORMAL = "normal"
    RECONCILING = "reconciling"


class MailSyncDisposition(StrEnum):
    """What happened to one fetched message."""

    NEW_MESSAGE = "new_message"
    MATCHED_MESSAGE = "matched_message"
    EXISTING_LOCATION = "existing_location"


@dataclass(frozen=True, slots=True)
class ParsedAttachment:
    """One attachment's metadata, as parsed from a message."""

    ordinal: int
    size_bytes: int
    sha256: str
    filename: str | None = None
    content_type: str | None = None
    content_disposition: str | None = None

    def __post_init__(self) -> None:
        if self.ordinal < 0:
            raise InvalidMailMessage("attachment ordinal must not be negative")
        if self.size_bytes < 0:
            raise InvalidMailMessage("attachment size must not be negative")
        if not SHA256_PATTERN.match(self.sha256):
            raise InvalidMailMessage("attachment sha256 must be 64 lowercase hex characters")


@dataclass(frozen=True, slots=True)
class ParsedMail:
    """Everything a parser could read from one message, plus how cleanly it read it."""

    message_id_header: str | None = None
    in_reply_to_header: str | None = None
    references: tuple[str, ...] = ()
    subject: str | None = None
    from_address: str | None = None
    to_addresses: tuple[str, ...] = ()
    cc_addresses: tuple[str, ...] = ()
    reply_to_addresses: tuple[str, ...] = ()
    date_header: str | None = None
    sent_at: datetime | None = None
    body_text: str | None = None
    attachments: tuple[ParsedAttachment, ...] = ()
    warnings: int = 0
    warnings_detail: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MailAttachmentMetadata:
    """What is known about one attachment, without materialising it.

    The bytes stay inside the raw `.eml` object: Phase 5A records evidence, it does not unpack
    files, and the filename is never used as a path.
    """

    message_id: MailMessageId
    ordinal: int
    size_bytes: int
    sha256: str
    id: UUID = field(default_factory=uuid4)
    filename: str | None = None
    content_type: str | None = None
    content_disposition: str | None = None

    def __post_init__(self) -> None:
        if self.ordinal < 0:
            raise InvalidMailMessage("attachment ordinal must not be negative")
        if self.size_bytes < 0:
            raise InvalidMailMessage("attachment size must not be negative")
        if not SHA256_PATTERN.match(self.sha256):
            raise InvalidMailMessage("attachment sha256 must be 64 lowercase hex characters")
        if self.filename is not None:
            normalized = normalize_header_value(self.filename, limit=MAX_FILENAME_CHARS)
            if normalized is None:
                raise InvalidMailMessage("an attachment filename must not be blank")
            object.__setattr__(self, "filename", normalized)


@dataclass(frozen=True, slots=True)
class MailMessage:
    """One logical inbound message, independent of where it was seen."""

    account_id: MailAccountId
    content_fingerprint: str
    size_bytes: int
    first_seen_at: datetime
    last_seen_at: datetime
    id: MailMessageId = field(default_factory=uuid4)
    message_id_header: str | None = None
    in_reply_to_header: str | None = None
    references: tuple[str, ...] = ()
    subject: str | None = None
    from_address: str | None = None
    to_addresses: tuple[str, ...] = ()
    cc_addresses: tuple[str, ...] = ()
    reply_to_addresses: tuple[str, ...] = ()
    date_header: str | None = None
    sent_at: datetime | None = None
    body_text: str | None = None
    body_status: MailBodyStatus = MailBodyStatus.AVAILABLE
    raw_sha256: str | None = None
    raw_storage_key: str | None = None
    parse_warnings: int = 0

    def __post_init__(self) -> None:
        validate_account_id(self.account_id)
        if self.size_bytes < 0:
            raise InvalidMailMessage("message size must not be negative")
        if not SHA256_PATTERN.match(self.content_fingerprint):
            raise InvalidMailMessage(
                "content_fingerprint must be 64 lowercase hex characters"
            )
        if self.raw_sha256 is not None and not SHA256_PATTERN.match(self.raw_sha256):
            raise InvalidMailMessage("raw_sha256 must be 64 lowercase hex characters")
        _require_aware(self.first_seen_at, "first_seen_at")
        _require_aware(self.last_seen_at, "last_seen_at")
        if self.sent_at is not None:
            _require_aware(self.sent_at, "sent_at")
        if len(self.references) > MAX_REFERENCES:
            raise InvalidMailMessage(
                f"a message may carry at most {MAX_REFERENCES} references"
            )
        for addresses, field_name in (
            (self.to_addresses, "to_addresses"),
            (self.cc_addresses, "cc_addresses"),
            (self.reply_to_addresses, "reply_to_addresses"),
        ):
            if len(addresses) > MAX_ADDRESSES:
                raise InvalidMailMessage(f"{field_name} may hold at most {MAX_ADDRESSES} entries")
        if self.body_status is MailBodyStatus.AVAILABLE:
            if self.body_text is None and self.raw_sha256 is None:
                raise InvalidMailMessage(
                    "an AVAILABLE body needs either text or a stored raw message"
                )
        elif self.body_text is not None:
            raise InvalidMailMessage("an OVERSIZE message must not carry body text")
        if self.raw_storage_key is not None and self.raw_sha256 is None:
            raise InvalidMailMessage("a raw storage key needs a raw sha256")

    @property
    def body_available(self) -> bool:
        """Whether the message body is readable locally."""
        return self.body_status is MailBodyStatus.AVAILABLE


@dataclass(frozen=True, slots=True)
class MailMessageLocation:
    """One place a logical message was seen: the identity an IMAP UID actually has."""

    message_id: MailMessageId
    account_id: MailAccountId
    mailbox_name: str
    uidvalidity: int
    uid: int
    first_seen_at: datetime
    last_seen_at: datetime

    def __post_init__(self) -> None:
        validate_account_id(self.account_id)
        if not self.mailbox_name.strip():
            raise InvalidMailMessage("a location needs a mailbox name")
        if self.uidvalidity < 1:
            raise InvalidMailboxState("uidvalidity must be a positive integer")
        if self.uid < 1:
            raise InvalidMailboxState("uid must be a positive integer")
        _require_aware(self.first_seen_at, "first_seen_at")
        _require_aware(self.last_seen_at, "last_seen_at")

    @property
    def identity(self) -> tuple[str, str, int, int]:
        """The durable identity of this location: `(account, mailbox, uidvalidity, uid)`."""
        return (self.account_id, self.mailbox_name, self.uidvalidity, self.uid)


@dataclass(frozen=True, slots=True)
class MailboxSyncState:
    """The incremental state of one mailbox: a cursor, not a proof of completeness."""

    account_id: MailAccountId
    mailbox_name: str
    uidvalidity: int
    last_seen_uid: int
    updated_at: datetime
    mode: MailboxSyncMode = MailboxSyncMode.NORMAL
    last_sync_at: datetime | None = None
    last_reconciled_at: datetime | None = None

    def __post_init__(self) -> None:
        validate_account_id(self.account_id)
        if not self.mailbox_name.strip():
            raise InvalidMailboxState("a sync state needs a mailbox name")
        if self.uidvalidity < 1:
            raise InvalidMailboxState("uidvalidity must be a positive integer")
        if self.last_seen_uid < 0:
            raise InvalidMailboxState("last_seen_uid must not be negative")
        _require_aware_state(self.updated_at, "updated_at")
        for value, field_name in (
            (self.last_sync_at, "last_sync_at"),
            (self.last_reconciled_at, "last_reconciled_at"),
        ):
            if value is not None:
                _require_aware_state(value, field_name)

    def next_uid_to_fetch(self) -> int:
        """The first UID the next incremental sync should ask for."""
        return self.last_seen_uid + 1


def mail_content_fingerprint(
    *,
    message_id_header: str | None,
    from_address: str | None,
    date_header: str | None,
    subject: str | None,
    body_text: str | None,
    attachment_sha256s: tuple[str, ...],
    size_bytes: int,
) -> str:
    """Canonical fingerprint of the parsed *logical* message.

    Deliberately distinct from the raw SHA-256: this describes what the message says, so a
    mailbox rebuilt with different byte-level encoding but the same content can still be
    recognised. Canonical JSON in, SHA-256 out.
    """
    payload = {
        "message_id": message_id_header,
        "from": from_address,
        "date": date_header,
        "subject": subject,
        "body_text": body_text,
        "attachments": list(attachment_sha256s),
        "size_bytes": size_bytes,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ReconciliationEvidence:
    """What a freshly fetched message can say about which message it might be."""

    content_fingerprint: str
    size_bytes: int
    raw_sha256: str | None = None
    message_id_header: str | None = None
    from_address: str | None = None
    date_header: str | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationDecision:
    """Whether reconciliation may reuse an existing message, and why not."""

    matched_message_id: MailMessageId | None = None
    conflicted: bool = False

    @property
    def is_match(self) -> bool:
        """Whether a stable message was recognised."""
        return self.matched_message_id is not None


@dataclass(frozen=True, slots=True)
class ReconciliationCandidates:
    """Candidate stable messages the store found for one fingerprint/header."""

    by_raw_sha256: MailMessage | None = None
    by_message_id: tuple[MailMessage, ...] = ()
    by_fingerprint: tuple[MailMessage, ...] = ()


def choose_reconciliation_match(
    evidence: ReconciliationEvidence, candidates: ReconciliationCandidates
) -> ReconciliationDecision:
    """Decide whether a rebuilt mailbox still holds a message we already have.

    Only strong evidence merges: identical raw bytes, or the same Message-ID together with the
    same content fingerprint. A fingerprint that matches while the Message-ID differs is accepted
    only when the cheap corroborating fields (size, sender, date header) agree too. The same
    Message-ID with a *different* fingerprint is a conflict: two real messages can share a
    Message-ID, and merging them would destroy one of them.
    """
    if evidence.raw_sha256 is not None:
        same_bytes = candidates.by_raw_sha256
        if same_bytes is not None and same_bytes.raw_sha256 == evidence.raw_sha256:
            return ReconciliationDecision(matched_message_id=same_bytes.id)
    header = normalize_message_id(evidence.message_id_header)
    if header is not None:
        with_same_header = [
            candidate
            for candidate in candidates.by_message_id
            if candidate.message_id_header == header
        ]
        for candidate in with_same_header:
            if candidate.content_fingerprint == evidence.content_fingerprint:
                return ReconciliationDecision(matched_message_id=candidate.id)
        if with_same_header:
            # The same Message-ID exists with different content: never merge, and say so.
            return ReconciliationDecision(conflicted=True)
    for candidate in candidates.by_fingerprint:
        if candidate.content_fingerprint != evidence.content_fingerprint:
            continue
        if (
            candidate.size_bytes == evidence.size_bytes
            and candidate.from_address == evidence.from_address
            and candidate.date_header == evidence.date_header
        ):
            return ReconciliationDecision(matched_message_id=candidate.id)
    return ReconciliationDecision()


__all__ = [
    "ACCOUNT_ID_PATTERN",
    "MAX_ADDRESSES",
    "MAX_MESSAGE_ID_CHARS",
    "MAX_REFERENCES",
    "MAX_SUBJECT_CHARS",
    "MailAccountId",
    "MailAttachmentMetadata",
    "MailBodyStatus",
    "MailMessage",
    "MailMessageId",
    "MailMessageLocation",
    "MailSyncDisposition",
    "MailboxSyncMode",
    "MailboxSyncState",
    "ParsedAttachment",
    "ParsedMail",
    "ReconciliationCandidates",
    "ReconciliationDecision",
    "ReconciliationEvidence",
    "choose_reconciliation_match",
    "mail_content_fingerprint",
    "new_mail_message_id",
    "normalize_header_value",
    "normalize_message_id",
    "validate_account_id",
]
