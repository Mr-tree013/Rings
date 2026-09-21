"""ContactRepository port: durable local identity records (ADR-0037 §9).

A contact is one row with one meaning: a name, an address and a lifecycle. Nothing here reads or
writes mail, a fact, a credential or a conversation, and nothing here exposes a connection, a
transaction or SQL.
"""

from __future__ import annotations

from typing import Protocol

from assistant.domain.contact import Contact, ContactId, ContactStatus


class ContactRepository(Protocol):
    """Storage for the contacts a name may resolve to."""

    async def add_contact(self, contact: Contact) -> Contact:
        """Store a new contact.

        Raises:
            CommitmentStoreError: a live contact with the same name and address already exists.
        """
        ...

    async def update_contact(self, contact: Contact) -> Contact:
        """Store the current state of an existing contact."""
        ...

    async def get_contact(self, contact_id: ContactId) -> Contact | None:
        """Return one contact, or `None`."""
        ...

    async def find_active_by_fingerprint(self, fingerprint: str) -> Contact | None:
        """The live contact with this exact name and address, if there is one."""
        ...

    async def list_contacts(
        self, *, status: ContactStatus | None = ContactStatus.ACTIVE
    ) -> list[Contact]:
        """Contacts with one status, oldest first (or every contact when `status` is `None`)."""
        ...


__all__ = ["ContactRepository"]
