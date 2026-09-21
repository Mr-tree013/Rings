"""Contacts: local structured identity, used to turn a name into an address (ADR-0037).

```text
"张老师"  ──► Contact(display_name="张老师", email_address="zhang@example.edu")
                     │
                     └─► the resolver's *only* non-explicit source of a recipient address
```

A contact is deliberately narrow. It is not memory (`ConfirmedFact`), it is not authority (it
grants no capability and creates no `ActionRequest`), and it holds no credential, no message body
and no conversation text. It is the local answer to one question a person asks out loud: *which
address does this name mean?*

Two people may share a name, one address may carry more than one label, and an exact live duplicate
(same name, same address) is one contact. Both "the same name" and "the same address" are defined
by normalization here rather than by a database collation, so a lookup behaves the same on every
host.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from assistant.domain.errors import InvalidContact
from assistant.domain.mail_draft import mailbox_address

ContactId = UUID
"""Stable identity of one local contact."""

MAX_CONTACT_NAME_CHARS = 120
MAX_CONTACT_ADDRESS_CHARS = 320

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def new_contact_id() -> ContactId:
    """Generate a fresh contact identity."""
    return uuid4()


class ContactStatus(StrEnum):
    """Whether the contact is still a usable answer for a name."""

    ACTIVE = "active"
    RETIRED = "retired"


def contact_name_key(display_name: str) -> str:
    """What "the same name" means: whitespace-collapsed, case-folded, nothing else.

    Case folding (not lowercasing) is used because a name may be written in any script; the
    database stores the same value in `display_name`, and the fingerprint is computed from this
    key, so the comparison never depends on a collation.
    """
    return " ".join(display_name.split()).casefold()


def normalize_contact_address(value: str) -> str | None:
    """The bare `local@domain` a contact stores, or `None` when the value is unusable.

    The existing mail address parser does the work (display names, angle brackets and comments are
    understood), and the result must then be plain ASCII: an address key has to compare the same
    way on every host, and an IDN domain is written in its ASCII form.
    """
    address = mailbox_address(value)
    if address is None or not address.isascii():
        return None
    if len(address) > MAX_CONTACT_ADDRESS_CHARS:
        return None
    return address


def contact_address_key(email_address: str) -> str:
    """The stored lookup key of a contact address.

    Raises:
        InvalidContact: the value is not a usable address.
    """
    address = normalize_contact_address(email_address)
    if address is None:
        raise InvalidContact(f"{email_address!r} is not a usable mail address")
    return address.lower()


def normalize_contact_name(display_name: str) -> str:
    """The stored display name: whitespace-collapsed and bounded.

    Raises:
        InvalidContact: the name is blank or too long.
    """
    name = " ".join(display_name.split())
    if not name:
        raise InvalidContact("a contact needs a name")
    if len(name) > MAX_CONTACT_NAME_CHARS:
        raise InvalidContact(
            f"a contact name must be at most {MAX_CONTACT_NAME_CHARS} characters"
        )
    return name


def contact_fingerprint(display_name: str, email_address: str) -> str:
    """Canonical SHA-256 over the meaning of one contact: its name and its address.

    Identity and lifecycle are deliberately absent: renaming or re-addressing is a new meaning for
    the same contact id, and retirement never changes what the contact *was*.
    """
    document = {
        "name": contact_name_key(display_name),
        "email": contact_address_key(email_address),
    }
    encoded = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class Contact:
    """One local name → address record. Not a fact, not an address book import."""

    display_name: str
    email_address: str
    created_at: datetime
    updated_at: datetime
    id: ContactId = field(default_factory=new_contact_id)
    status: ContactStatus = ContactStatus.ACTIVE
    retired_at: datetime | None = None

    def __post_init__(self) -> None:
        name = normalize_contact_name(self.display_name)
        address = normalize_contact_address(self.email_address)
        if address is None:
            raise InvalidContact(f"{self.email_address!r} is not a usable mail address")
        object.__setattr__(self, "display_name", name)
        object.__setattr__(self, "email_address", address)
        for value, field_name in (
            (self.created_at, "created_at"),
            (self.updated_at, "updated_at"),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise InvalidContact(f"{field_name} must be timezone-aware")
        if self.updated_at < self.created_at:
            raise InvalidContact("updated_at must not precede created_at")
        if self.retired_at is not None and (
            self.retired_at.tzinfo is None or self.retired_at.utcoffset() is None
        ):
            raise InvalidContact("retired_at must be timezone-aware")
        if (self.status is ContactStatus.RETIRED) != (self.retired_at is not None):
            raise InvalidContact("retired_at is set exactly when the contact is retired")

    @property
    def email_key(self) -> str:
        """The case-folded address this contact is looked up by."""
        return self.email_address.lower()

    @property
    def name_key(self) -> str:
        """The case-folded name this contact is looked up by."""
        return contact_name_key(self.display_name)

    @property
    def fingerprint(self) -> str:
        """The canonical identity of this contact's meaning."""
        return contact_fingerprint(self.display_name, self.email_address)

    @property
    def is_active(self) -> bool:
        """Whether this contact can still resolve a name."""
        return self.status is ContactStatus.ACTIVE

    def edited(
        self,
        *,
        display_name: str | None = None,
        email_address: str | None = None,
        at: datetime,
    ) -> Contact:
        """Return the same contact with a new name and/or address.

        Raises:
            InvalidContact: the new values break an invariant, or the contact is retired.
        """
        if at.tzinfo is None or at.utcoffset() is None:
            raise InvalidContact("at must be timezone-aware")
        if self.status is not ContactStatus.ACTIVE:
            raise InvalidContact("a retired contact cannot be edited")
        return Contact(
            id=self.id,
            display_name=self.display_name if display_name is None else display_name,
            email_address=self.email_address if email_address is None else email_address,
            status=ContactStatus.ACTIVE,
            retired_at=None,
            created_at=self.created_at,
            updated_at=at,
        )

    def retired(self, at: datetime) -> Contact:
        """Return the same contact, retired. Nothing is deleted."""
        if at.tzinfo is None or at.utcoffset() is None:
            raise InvalidContact("at must be timezone-aware")
        return Contact(
            id=self.id,
            display_name=self.display_name,
            email_address=self.email_address,
            status=ContactStatus.RETIRED,
            retired_at=at,
            created_at=self.created_at,
            updated_at=at,
        )


__all__ = [
    "MAX_CONTACT_ADDRESS_CHARS",
    "MAX_CONTACT_NAME_CHARS",
    "Contact",
    "ContactId",
    "ContactStatus",
    "contact_address_key",
    "contact_fingerprint",
    "contact_name_key",
    "new_contact_id",
    "normalize_contact_address",
    "normalize_contact_name",
]
