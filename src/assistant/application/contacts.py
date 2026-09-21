"""Contacts as durable local identity records (ADR-0037 §9).

Creating the same exact contact twice is idempotent: the canonical fingerprint of (name, address)
is what identifies it, so recording one name and address three times leaves one contact, not
three. Editing or retiring never deletes anything, and nothing here creates a `Correction`, a
`FactCandidate` or a `ConfirmedFact` — a contact is not personal memory.
"""

from __future__ import annotations

from assistant.domain.contact import (
    Contact,
    ContactId,
    ContactStatus,
    contact_name_key,
    normalize_contact_name,
)
from assistant.domain.errors import (
    AmbiguousId,
    ContactNotFound,
    InvalidContact,
)
from assistant.ports.clock import Clock
from assistant.ports.contact_repository import ContactRepository


class ContactService:
    """Create, inspect, edit and retire the contacts a name may resolve to."""

    def __init__(self, repository: ContactRepository, clock: Clock) -> None:
        self._contacts = repository
        self._clock = clock

    async def create(
        self, *, display_name: str, email_address: str
    ) -> tuple[Contact, bool]:
        """Create one contact, or return the existing one and say which happened.

        The flag is what lets a caller answer "这个联系人已经存在" instead of reporting a creation
        that did not happen.

        Raises:
            InvalidContact: the name or address is unusable.
        """
        now = self._clock.now()
        candidate = Contact(
            display_name=display_name,
            email_address=email_address,
            created_at=now,
            updated_at=now,
        )
        existing = await self._contacts.find_active_by_fingerprint(candidate.fingerprint)
        if existing is not None:
            return existing, False
        return await self._contacts.add_contact(candidate), True

    async def list_active(self) -> list[Contact]:
        """Every live contact, oldest first."""
        return await self._contacts.list_contacts(status=ContactStatus.ACTIVE)

    async def list_contacts(
        self, *, include_retired: bool = False
    ) -> list[Contact]:
        """Every live contact, retired ones only when they were asked for."""
        return await self._contacts.list_contacts(
            status=None if include_retired else ContactStatus.ACTIVE
        )

    async def require_contact(self, reference: str) -> Contact:
        """Return one contact by id or unique prefix.

        Raises:
            ContactNotFound: nothing matches.
            InvalidContact: the prefix is ambiguous.
        """
        contact = await self._contacts.get_contact(await self.resolve_contact_id(reference))
        if contact is None:  # pragma: no cover - resolution just found it
            raise ContactNotFound(reference)
        return contact

    async def resolve_contact_id(self, reference: str) -> ContactId:
        """Resolve a full id or a unique prefix.

        Raises:
            ContactNotFound: nothing matches.
            InvalidContact: the prefix is ambiguous.
        """
        cleaned = reference.strip().lower()
        if not cleaned:
            raise ContactNotFound(reference)
        contacts = await self._contacts.list_contacts(status=None)
        matches = [contact for contact in contacts if str(contact.id).startswith(cleaned)]
        if not matches:
            raise ContactNotFound(reference)
        if len(matches) > 1:
            raise AmbiguousId(reference, len(matches))
        return matches[0].id

    async def find_by_name(self, name: str) -> list[Contact]:
        """Every live contact whose name matches, in a stable order.

        Raises:
            InvalidContact: the name is blank.
        """
        wanted = contact_name_key(normalize_contact_name(name))
        return [
            contact
            for contact in await self.list_active()
            if contact.name_key == wanted
        ]

    async def edit(
        self,
        reference: str,
        *,
        display_name: str | None = None,
        email_address: str | None = None,
    ) -> Contact:
        """Change one contact's name and/or address, keeping its identity.

        Raises:
            ContactNotFound: no such contact.
            InvalidContact: the change breaks an invariant or collides with another live contact.
        """
        current = await self.require_contact(reference)
        if display_name is None and email_address is None:
            raise InvalidContact("a contact edit needs at least one field to change")
        updated = current.edited(
            display_name=display_name,
            email_address=email_address,
            at=self._clock.now(),
        )
        conflict = await self._contacts.find_active_by_fingerprint(updated.fingerprint)
        if conflict is not None and conflict.id != updated.id:
            raise InvalidContact(
                f"已经有一条同样的联系人了 (id {str(conflict.id)[:8]})"
            )
        return await self._contacts.update_contact(updated)

    async def retire(self, reference: str) -> Contact:
        """Retire one contact: it stops resolving a name, and history is untouched."""
        current = await self.require_contact(reference)
        if current.status is ContactStatus.RETIRED:
            return current
        return await self._contacts.update_contact(
            current.retired(self._clock.now())
        )


__all__ = ["ContactService"]
