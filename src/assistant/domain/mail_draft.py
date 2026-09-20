"""Durable reply drafts: recipients, subject and body as local decisions (ADR-0022).

A reply draft has three parts, and only one of them is a model's:

- **who it goes to is derived, never generated.** `Reply-To` wins over `From`, and both are parsed
  with the stdlib address parser so a display name containing a comma cannot split an address.
  When neither yields a usable mailbox, drafting stops with `MailReplyRecipientUnavailable`
  rather than asking a model what the address probably was;
- **the subject is derived, never generated.** `reply_subject` is a pure function: an existing
  `Re:` prefix is preserved, a missing or blank subject becomes `Re:`, and anything else becomes
  `Re: <subject>`;
- **the body is the model's only output**, and it is stored as a draft — never as something that
  has been sent. There is no `sent`, `approved` or `ActionRequest` field anywhere in this module,
  because this phase has no sending path at all.

An edit is optimistic: it must present the version it started from, and a mismatch is refused
rather than silently overwriting whatever the user (or another window) wrote in the meantime.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parseaddr
from enum import StrEnum
from uuid import UUID, uuid4

from assistant.domain.errors import (
    InvalidMailDraft,
    MailReplyRecipientUnavailable,
)
from assistant.domain.knowledge import SourceSpan, SourceSpanKind
from assistant.domain.mail import (
    MAX_ADDRESS_CHARS,
    MailAccountId,
    MailMessage,
    MailMessageId,
    validate_account_id,
)
from assistant.domain.mail_analysis import MailThreadId
from assistant.domain.storage import StorageUri

MailDraftId = UUID
"""Stable identity of one local draft."""

MAX_DRAFT_BODY_CHARS = 12000
MAX_DRAFT_SUBJECT_CHARS = 2000
MAX_DRAFT_RECIPIENTS = 50
MAX_USED_SOURCES = 8
MAX_NEEDS_USER_INPUT = 8
MAX_NEEDS_USER_INPUT_CHARS = 500
MAX_MAILBOX_CHARS = 320

PROMPT_VERSION = 1
"""Bump when the draft instructions or schema change in a way that changes results."""

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_RE_PREFIX_PATTERN = re.compile(r"^\s*re\s*:", re.IGNORECASE)


def new_mail_draft_id() -> MailDraftId:
    """Generate a fresh draft identity."""
    return uuid4()


class MailDraftOrigin(StrEnum):
    """Whether a draft body came from a model or from a person."""

    MODEL_GENERATED = "model_generated"
    USER_EDITED = "user_edited"


def reply_subject(original_subject: str | None) -> str:
    """The subject of a reply, derived locally and deterministically.

    A blank or missing subject becomes `Re:`; a subject that already starts with `Re:` (in any
    case) is preserved exactly, so a thread never accumulates `Re: Re:`; anything else gains the
    prefix and a separating space. The model is never asked for a subject.
    """
    candidate = "" if original_subject is None else " ".join(original_subject.split())
    if not candidate:
        return "Re:"
    if _RE_PREFIX_PATTERN.match(candidate):
        return candidate
    return f"Re: {candidate}"


def mailbox_address(value: str) -> str | None:
    """The bare `local@domain` inside one address header value, or `None`.

    The parser does the work (`email.utils.parseaddr` understands display names, angle brackets
    and comments), and the result is then checked against the characters an address may contain.
    A value that carries whitespace, control characters or a second `@` is rejected: mail arrives
    from strangers, and a header is not allowed to smuggle a second recipient into a later
    conversation.
    """
    text = value.strip()
    if not text or len(text) > MAX_ADDRESS_CHARS:
        return None
    _, address = parseaddr(text)
    address = address.strip()
    if not address or len(address) > MAX_MAILBOX_CHARS:
        return None
    if any(character.isspace() or ord(character) < 32 for character in address):
        return None
    if address.count("@") != 1:
        return None
    local, _, domain = address.partition("@")
    if not local or not domain or domain.startswith(".") or domain.endswith("."):
        return None
    return address


def resolve_reply_recipients(message: MailMessage) -> tuple[str, ...]:
    """Who a reply to `message` goes to, derived locally and in a fixed order.

    `Reply-To` is preferred over `From`, because that is the header whose entire purpose is to say
    where a reply belongs; V1 is a plain reply, so nothing else (no `Cc`, no other recipient of
    the original) is added.

    Raises:
        MailReplyRecipientUnavailable: neither header yields a usable mailbox.
    """
    for candidates in (message.reply_to_addresses, (message.from_address,)):
        resolved = _mailboxes(candidates)
        if resolved:
            return resolved
    raise MailReplyRecipientUnavailable(
        f"mail message {message.id} has no usable Reply-To or From address"
    )


def _mailboxes(values: Iterable[str | None]) -> tuple[str, ...]:
    found: list[str] = []
    for value in values:
        if value is None:
            continue
        address = mailbox_address(value)
        if address is not None and address not in found:
            found.append(address)
        if len(found) == MAX_DRAFT_RECIPIENTS:
            break
    return tuple(found)


@dataclass(frozen=True, slots=True)
class MailDraftEvidenceIdentity:
    """What one piece of supplied knowledge evidence *is*, for the audit fingerprint.

    Deliberately not the excerpt: a fingerprint is an identity, and the index already owns the
    text. The content hash is taken over the exact bounded excerpt the model was given.
    """

    root_id: str
    chunk_id: str
    logical_uri: str
    source_span: str
    content_sha256: str


def mail_draft_input_fingerprint(
    *,
    prompt_version: int,
    schema_version: int,
    reply_to_message_id: MailMessageId,
    to_addresses: tuple[str, ...],
    subject: str,
    thread_message_ids: tuple[MailMessageId, ...],
    thread_content_fingerprints: tuple[str, ...],
    context_query: str | None,
    root_id: str | None,
    evidence: tuple[MailDraftEvidenceIdentity, ...],
) -> str:
    """Canonical fingerprint of exactly what a draft was computed from.

    Canonical JSON in, SHA-256 out. This is an audit and debugging artefact: it records *which*
    question produced a draft so a later phase can tell "the same request" from "a different one".
    It is never a reason to send anything automatically.
    """
    payload = {
        "prompt_version": prompt_version,
        "schema_version": schema_version,
        "reply_to_message_id": str(reply_to_message_id),
        "to_addresses": list(to_addresses),
        "subject": subject,
        "thread_message_ids": [str(value) for value in thread_message_ids],
        "thread_content_fingerprints": list(thread_content_fingerprints),
        "context_query": context_query,
        "root_id": root_id,
        "evidence": [
            {
                "root_id": item.root_id,
                "chunk_id": item.chunk_id,
                "logical_uri": item.logical_uri,
                "source_span": item.source_span,
                "content_sha256": item.content_sha256,
            }
            for item in evidence
        ],
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MailDraft:
    """One local reply draft. Not an outbox, not an approval, not a sent message."""

    account_id: MailAccountId
    reply_to_message_id: MailMessageId
    to_addresses: tuple[str, ...]
    subject: str
    body_text: str
    generation_input_fingerprint: str
    created_at: datetime
    updated_at: datetime
    id: MailDraftId = field(default_factory=new_mail_draft_id)
    thread_id: MailThreadId | None = None
    needs_user_input: tuple[str, ...] = ()
    origin: MailDraftOrigin = MailDraftOrigin.MODEL_GENERATED
    version: int = 1
    prompt_version: int = PROMPT_VERSION

    def __post_init__(self) -> None:
        validate_account_id(self.account_id)
        if not self.to_addresses:
            raise InvalidMailDraft("a draft needs at least one recipient")
        if len(self.to_addresses) > MAX_DRAFT_RECIPIENTS:
            raise InvalidMailDraft(
                f"a draft may have at most {MAX_DRAFT_RECIPIENTS} recipients"
            )
        subject = self.subject.strip()
        if not subject:
            raise InvalidMailDraft("a draft needs a subject")
        if len(subject) > MAX_DRAFT_SUBJECT_CHARS:
            raise InvalidMailDraft(
                f"a draft subject must be at most {MAX_DRAFT_SUBJECT_CHARS} characters"
            )
        object.__setattr__(self, "subject", subject)
        body = self.body_text.strip()
        if not body:
            raise InvalidMailDraft("a draft needs a body")
        if len(body) > MAX_DRAFT_BODY_CHARS:
            raise InvalidMailDraft(
                f"a draft body must be at most {MAX_DRAFT_BODY_CHARS} characters"
            )
        object.__setattr__(self, "body_text", body)
        if len(self.needs_user_input) > MAX_NEEDS_USER_INPUT:
            raise InvalidMailDraft(
                f"a draft may carry at most {MAX_NEEDS_USER_INPUT} open questions"
            )
        cleaned: list[str] = []
        for item in self.needs_user_input:
            question = " ".join(item.split())
            if not question:
                raise InvalidMailDraft("an open question must not be blank")
            if len(question) > MAX_NEEDS_USER_INPUT_CHARS:
                raise InvalidMailDraft(
                    f"an open question must be at most {MAX_NEEDS_USER_INPUT_CHARS} characters"
                )
            cleaned.append(question)
        object.__setattr__(self, "needs_user_input", tuple(cleaned))
        if self.version < 1:
            raise InvalidMailDraft("a draft version must be at least 1")
        if self.prompt_version < 1:
            raise InvalidMailDraft("a draft prompt version must be at least 1")
        if not _SHA256_PATTERN.match(self.generation_input_fingerprint):
            raise InvalidMailDraft(
                "a draft input fingerprint must be 64 lowercase hex characters"
            )
        for value, name in ((self.created_at, "created_at"), (self.updated_at, "updated_at")):
            if value.tzinfo is None or value.utcoffset() is None:
                raise InvalidMailDraft(f"{name} must be timezone-aware")

    @property
    def is_edited(self) -> bool:
        """Whether a person has touched this draft's body or subject."""
        return self.origin is MailDraftOrigin.USER_EDITED


@dataclass(frozen=True, slots=True)
class MailDraftPlan:
    """Everything about a draft that is decided locally, before any model is asked for prose.

    Recipients, subject, thread and the audit fingerprint are all fixed here. The model's only
    contribution is the body, which `mail_draft_from_plan` adds — so "the model does not choose
    who this goes to" is a property of the shape, not a rule someone has to remember.
    """

    account_id: MailAccountId
    reply_to_message_id: MailMessageId
    to_addresses: tuple[str, ...]
    subject: str
    generation_input_fingerprint: str
    thread_id: MailThreadId | None = None

    def __post_init__(self) -> None:
        if not self.to_addresses:
            raise InvalidMailDraft("a draft plan needs at least one recipient")
        if not self.subject.strip():
            raise InvalidMailDraft("a draft plan needs a subject")
        if not _SHA256_PATTERN.match(self.generation_input_fingerprint):
            raise InvalidMailDraft(
                "a draft input fingerprint must be 64 lowercase hex characters"
            )


def mail_draft_from_plan(
    plan: MailDraftPlan,
    *,
    body_text: str,
    needs_user_input: tuple[str, ...] = (),
    origin: MailDraftOrigin = MailDraftOrigin.MODEL_GENERATED,
    at: datetime,
    draft_id: MailDraftId | None = None,
    version: int = 1,
) -> MailDraft:
    """Combine the local decisions with a body into a draft."""
    return MailDraft(
        id=new_mail_draft_id() if draft_id is None else draft_id,
        account_id=plan.account_id,
        thread_id=plan.thread_id,
        reply_to_message_id=plan.reply_to_message_id,
        to_addresses=plan.to_addresses,
        subject=plan.subject,
        body_text=body_text,
        needs_user_input=needs_user_input,
        origin=origin,
        version=version,
        generation_input_fingerprint=plan.generation_input_fingerprint,
        created_at=at,
        updated_at=at,
    )


@dataclass(frozen=True, slots=True)
class MailDraftSource:
    """One knowledge source the model says it used, resolved from the supplied evidence."""

    draft_id: MailDraftId
    root_id: str
    entry_id: UUID
    chunk_id: UUID
    logical_uri: StorageUri
    source_span: SourceSpan
    ordinal: int = 0

    def __post_init__(self) -> None:
        if self.ordinal < 0:
            raise InvalidMailDraft("a source ordinal must not be negative")
        if self.logical_uri.root_id != self.root_id:
            raise InvalidMailDraft(
                "a draft source's logical URI and root id must describe the same root"
            )

    def to_payload(self) -> dict[str, object]:
        """The JSON representation stored in the database."""
        return {
            "root_id": self.root_id,
            "entry_id": str(self.entry_id),
            "chunk_id": str(self.chunk_id),
            "logical_uri": str(self.logical_uri),
            "source_span": span_to_payload(self.source_span),
        }

    @classmethod
    def from_payload(
        cls,
        draft_id: MailDraftId,
        ordinal: int,
        root_id: str,
        payload: object,
    ) -> MailDraftSource:
        """Rebuild one source from its stored columns."""
        if not isinstance(payload, dict):
            raise InvalidMailDraft("a stored draft source must be a JSON object")
        try:
            return cls(
                draft_id=draft_id,
                root_id=root_id,
                entry_id=UUID(str(payload["entry_id"])),
                chunk_id=UUID(str(payload["chunk_id"])),
                logical_uri=StorageUri.parse(str(payload["logical_uri"])),
                source_span=span_from_payload(payload["source_span"]),
                ordinal=ordinal,
            )
        except KeyError as exc:
            raise InvalidMailDraft(f"stored draft source is missing {exc}") from exc
        except ValueError as exc:
            raise InvalidMailDraft(f"stored draft source is not readable: {exc}") from exc


def span_to_payload(span: SourceSpan) -> dict[str, object]:
    """A source location as data: a page, or a line range — never both."""
    if span.kind is SourceSpanKind.PAGE:
        return {"kind": "page", "page_number": span.page_number}
    return {"kind": "line", "line_start": span.line_start, "line_end": span.line_end}


def span_from_payload(payload: object) -> SourceSpan:
    """Rebuild a source location from its stored JSON."""
    if not isinstance(payload, dict):
        raise InvalidMailDraft("a stored source span must be a JSON object")
    if payload.get("kind") == "page":
        return SourceSpan.page(int(str(payload["page_number"])))
    return SourceSpan.lines(int(str(payload["line_start"])), int(str(payload["line_end"])))


__all__ = [
    "MAX_DRAFT_BODY_CHARS",
    "MAX_DRAFT_RECIPIENTS",
    "MAX_DRAFT_SUBJECT_CHARS",
    "MAX_NEEDS_USER_INPUT",
    "MAX_NEEDS_USER_INPUT_CHARS",
    "MAX_USED_SOURCES",
    "PROMPT_VERSION",
    "MailDraft",
    "MailDraftEvidenceIdentity",
    "MailDraftId",
    "MailDraftOrigin",
    "MailDraftPlan",
    "MailDraftSource",
    "mail_draft_from_plan",
    "mail_draft_input_fingerprint",
    "mailbox_address",
    "new_mail_draft_id",
    "reply_subject",
    "resolve_reply_recipients",
    "span_from_payload",
    "span_to_payload",
]
