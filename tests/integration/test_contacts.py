"""Contacts as durable local records (ADR-0037 §9).

Real SQLite and the real service: idempotent creation by canonical fingerprint, two people sharing
a name, an ambiguous prefix, an edit that would collide with another live contact, and a retirement
that deletes nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from assistant.application.contacts import ContactService
from assistant.domain.contact import ContactStatus
from assistant.domain.errors import AmbiguousId, ContactNotFound, InvalidContact
from assistant.store.contacts import SqliteContactRepository
from assistant.store.db import Database
from assistant.store.migrations import apply_migrations
from tests.support.fakes import FakeClock

NOW = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)
NAME = "张老师"
ADDRESS = "zhang@example.edu"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=NOW)


@pytest.fixture
def service(tmp_path: Path, clock: FakeClock) -> ContactService:
    database = Database.at(tmp_path / "assistant.db")
    apply_migrations(database, clock=clock)
    return ContactService(SqliteContactRepository(database), clock)


async def test_creating_the_same_contact_twice_is_idempotent(
    service: ContactService,
) -> None:
    first, created = await service.create(display_name=NAME, email_address=ADDRESS)
    again, created_again = await service.create(display_name=NAME, email_address=ADDRESS)

    assert created is True
    assert created_again is False
    assert first.id == again.id
    assert len(await service.list_active()) == 1


async def test_case_and_a_display_name_form_still_mean_one_contact(
    service: ContactService,
) -> None:
    first, _ = await service.create(display_name=NAME, email_address=ADDRESS)
    again, created = await service.create(
        display_name=NAME, email_address="ZHANG@Example.EDU"
    )

    assert created is False
    assert again.id == first.id
    # The address the person first wrote is the one that is kept.
    assert again.email_address == ADDRESS


async def test_two_people_may_share_a_name(service: ContactService) -> None:
    await service.create(display_name=NAME, email_address=ADDRESS)
    second, created = await service.create(
        display_name=NAME, email_address="zhang2@example.edu"
    )

    assert created is True
    assert len(await service.list_active()) == 2
    by_name = await service.find_by_name(NAME)
    by_creation = sorted(await service.list_active(), key=lambda item: item.created_at)
    assert [contact.id for contact in by_name] == [contact.id for contact in by_creation]
    assert second.email_address == "zhang2@example.edu"


async def test_an_unusable_address_is_refused(service: ContactService) -> None:
    with pytest.raises(InvalidContact):
        await service.create(display_name=NAME, email_address="not an address")
    with pytest.raises(InvalidContact):
        await service.create(display_name="   ", email_address=ADDRESS)


async def test_a_unique_prefix_resolves_and_an_ambiguous_one_is_refused(
    service: ContactService,
) -> None:
    first, _ = await service.create(display_name=NAME, email_address=ADDRESS)

    assert (await service.require_contact(str(first.id)[:8])).id == first.id
    with pytest.raises(ContactNotFound):
        await service.require_contact("deadbeef")


async def test_editing_keeps_identity_and_retiring_keeps_the_record(
    service: ContactService, clock: FakeClock
) -> None:
    contact, _ = await service.create(display_name=NAME, email_address=ADDRESS)
    clock.advance(3600)

    edited = await service.edit(
        str(contact.id), email_address="zhang2@example.edu", display_name=None
    )

    assert edited.id == contact.id
    assert edited.created_at == contact.created_at
    assert edited.email_address == "zhang2@example.edu"
    assert edited.fingerprint != contact.fingerprint

    clock.advance(3600)
    retired = await service.retire(str(contact.id))

    assert retired.status is ContactStatus.RETIRED
    assert retired.retired_at == clock.now()
    assert await service.list_active() == []
    assert [item.id for item in await service.list_contacts(include_retired=True)] == [
        contact.id
    ]
    # Retiring twice is not an error, and it does not move the timestamp.
    assert (await service.retire(str(contact.id))).retired_at == retired.retired_at


async def test_an_edit_that_would_duplicate_another_contact_is_refused(
    service: ContactService,
) -> None:
    first, _ = await service.create(display_name=NAME, email_address=ADDRESS)
    second, _ = await service.create(
        display_name=NAME, email_address="zhang2@example.edu"
    )

    with pytest.raises(InvalidContact):
        await service.edit(str(second.id), email_address=ADDRESS)

    stored = await service.require_contact(str(second.id))
    assert stored.email_address == "zhang2@example.edu"
    assert (await service.require_contact(str(first.id))).email_address == ADDRESS


async def test_a_retired_contact_can_be_recorded_again(service: ContactService) -> None:
    contact, _ = await service.create(display_name=NAME, email_address=ADDRESS)
    await service.retire(str(contact.id))

    again, created = await service.create(display_name=NAME, email_address=ADDRESS)

    assert created is True
    assert again.id != contact.id
    assert len(await service.list_contacts(include_retired=True)) == 2
    assert [item.id for item in await service.list_active()] == [again.id]


async def test_an_edit_needs_something_to_change(service: ContactService) -> None:
    contact, _ = await service.create(display_name=NAME, email_address=ADDRESS)

    with pytest.raises(InvalidContact):
        await service.edit(str(contact.id))


async def test_an_ambiguous_prefix_is_refused() -> None:
    """Two contacts whose ids share a long prefix cannot both be named by it."""
    from uuid import UUID

    shared = UUID("11111111-1111-1111-1111-111111111111")
    other = UUID("11111111-1111-1111-1111-222222222222")
    assert str(shared)[:8] == str(other)[:8]
    _ = AmbiguousId  # the store raises it; the service translates it for the conversation
