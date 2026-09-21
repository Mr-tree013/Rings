"""A durable draft of a *new* letter: subject, body and one recipient (ADR-0037).

A reply draft answers a stored message, so it keeps its source-message column and every invariant
that hangs off it. A new letter answers nothing, and pretending otherwise would have rewritten the
meaning of every historical reply row. This module is the narrow, separate half: one recipient, a
subject, a body — and a version that an edit must present, exactly like a reply draft.

Nothing here can send. There is no approval, no `ActionRequest`, no SMTP status and no send method;
a draft is local content until a human reviews an immutable action prepared from it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from uuid import UUID, uuid4

from assistant.domain.errors import InvalidNewMailDraft
from assistant.domain.mail import MailAccountId, validate_account_id
from assistant.domain.mail_draft import (
    MAX_DRAFT_BODY_CHARS,
    MAX_DRAFT_SUBJECT_CHARS,
    MailDraftOrigin,
    mailbox_address,
)

NewMailDraftId = UUID
"""Stable identity of one local new-mail draft."""

MAX_NEW_MAIL_RECIPIENTS = 1
"""v1.1 composes one primary recipient per letter. Cc and Bcc are not part of this phase."""


def new_new_mail_draft_id() -> NewMailDraftId:
    """Generate a fresh new-mail draft identity."""
    return uuid4()


@dataclass(frozen=True, slots=True)
class NewMailDraft:
    """One local draft of a new letter. Not an outbox, not an approval, not a sent message."""

    account_id: MailAccountId
    to_address: str
    subject: str
    body_text: str
    created_at: datetime
    updated_at: datetime
    id: NewMailDraftId = field(default_factory=new_new_mail_draft_id)
    origin: MailDraftOrigin = MailDraftOrigin.MODEL_GENERATED
    version: int = 1

    def __post_init__(self) -> None:
        validate_account_id(self.account_id)
        address = mailbox_address(self.to_address)
        if address is None or address != self.to_address.strip():
            raise InvalidNewMailDraft(
                f"a new mail needs one plain recipient address, not {self.to_address!r}"
            )
        object.__setattr__(self, "to_address", address)
        subject = " ".join(self.subject.split())
        if not subject:
            raise InvalidNewMailDraft("a new mail needs a subject")
        if len(subject) > MAX_DRAFT_SUBJECT_CHARS:
            raise InvalidNewMailDraft(
                f"a subject must be at most {MAX_DRAFT_SUBJECT_CHARS} characters"
            )
        object.__setattr__(self, "subject", subject)
        body = self.body_text.strip()
        if not body:
            raise InvalidNewMailDraft("a new mail needs a body")
        if len(body) > MAX_DRAFT_BODY_CHARS:
            raise InvalidNewMailDraft(
                f"a body must be at most {MAX_DRAFT_BODY_CHARS} characters"
            )
        object.__setattr__(self, "body_text", body)
        if self.version < 1:
            raise InvalidNewMailDraft("a draft version must be at least 1")
        for value, field_name in (
            (self.created_at, "created_at"),
            (self.updated_at, "updated_at"),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise InvalidNewMailDraft(f"{field_name} must be timezone-aware")

    @property
    def is_edited(self) -> bool:
        """Whether a person has touched this draft since it was written."""
        return self.origin is MailDraftOrigin.USER_EDITED

    def revised(
        self,
        *,
        at: datetime,
        account_id: str | None = None,
        to_address: str | None = None,
        subject: str | None = None,
        body_text: str | None = None,
    ) -> NewMailDraft:
        """Return the same draft, revised, at the next version.

        The version is what a later send snapshot is compared against, so an edit always advances
        it — a re-prepare after an edit is a new action rather than a second approval of the old
        text.
        """
        if at.tzinfo is None or at.utcoffset() is None:
            raise InvalidNewMailDraft("at must be timezone-aware")
        return replace(
            self,
            account_id=self.account_id if account_id is None else account_id,
            to_address=self.to_address if to_address is None else to_address,
            subject=self.subject if subject is None else subject,
            body_text=self.body_text if body_text is None else body_text,
            origin=MailDraftOrigin.USER_EDITED,
            version=self.version + 1,
            updated_at=at,
        )


__all__ = [
    "MAX_NEW_MAIL_RECIPIENTS",
    "NewMailDraft",
    "NewMailDraftId",
    "new_new_mail_draft_id",
]
