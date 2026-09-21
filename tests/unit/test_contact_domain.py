"""The contact domain: normalization, identity and lifecycle (ADR-0037 §8).

Pure types, so this is a text of values rather than of services: what "the same name" means, what
"the same address" means, what a fingerprint covers, and what a retirement may and may not do.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from assistant.domain.contact import (
    MAX_CONTACT_NAME_CHARS,
    Contact,
    ContactStatus,
    contact_address_key,
    contact_fingerprint,
    contact_name_key,
    normalize_contact_address,
    normalize_contact_name,
)
from assistant.domain.errors import InvalidContact

NOW = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)


def _contact(
    *,
    display_name: str = "张老师",
    email_address: str = "zhang@example.edu",
    at: datetime = NOW,
) -> Contact:
    return Contact(
        display_name=display_name,
        email_address=email_address,
        created_at=at,
        updated_at=at,
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("zhang@example.edu", "zhang@example.edu"),
        ("  zhang@example.edu  ", "zhang@example.edu"),
        ("张老师 <zhang@example.edu>", "zhang@example.edu"),
        ("ZHANG@EXAMPLE.EDU", "ZHANG@EXAMPLE.EDU"),
        ("not an address", None),
        ("", None),
        ("two@at@example.edu", None),
        ("no-domain@", None),
        ("@no-local.example.edu", None),
        ("域名@例子.中国", None),  # an address key has to compare the same way everywhere
    ),
)
def test_address_normalization(value: str, expected: str | None) -> None:
    assert normalize_contact_address(value) == expected


def test_the_address_key_is_case_folded_but_the_address_is_preserved() -> None:
    contact = _contact(email_address="Zhang@Example.EDU")

    assert contact.email_address == "Zhang@Example.EDU"
    assert contact.email_key == "zhang@example.edu"


def test_name_normalization_collapses_whitespace() -> None:
    assert normalize_contact_name("  张  老师 ") == "张 老师"
    assert contact_name_key("Zhang  Teacher") == contact_name_key("zhang teacher")
    assert contact_name_key("张老师") == "张老师"
    with pytest.raises(InvalidContact):
        normalize_contact_name("   ")
    with pytest.raises(InvalidContact):
        normalize_contact_name("x" * (MAX_CONTACT_NAME_CHARS + 1))


def test_the_fingerprint_covers_meaning_not_identity() -> None:
    first = _contact()
    same_meaning = _contact(email_address="ZHANG@EXAMPLE.EDU")
    other_person = _contact(email_address="zhang2@example.edu")
    same_address_other_name = _contact(display_name="张教授")
    spaced_name = _contact(display_name="张  老师")

    assert first.fingerprint == same_meaning.fingerprint
    assert first.fingerprint != other_person.fingerprint
    assert first.fingerprint != same_address_other_name.fingerprint
    # Whitespace runs collapse on the way in; a space between two characters is still a different
    # name, and the resolver asks rather than treating it as the same one.
    assert spaced_name.display_name == "张 老师"
    assert spaced_name.name_key == contact_name_key("张 老师")
    assert spaced_name.fingerprint != first.fingerprint
    assert first.fingerprint == contact_fingerprint("张老师", "ZHANG@example.edu")
    assert contact_address_key("ZHANG@example.edu") == "zhang@example.edu"


def test_a_blank_or_unusable_contact_is_refused() -> None:
    with pytest.raises(InvalidContact):
        _contact(display_name="  ")
    with pytest.raises(InvalidContact):
        _contact(email_address="not an address")


def test_retirement_keeps_the_contact_and_its_history() -> None:
    contact = _contact()

    retired = contact.retired(NOW + timedelta(hours=1))

    assert retired.id == contact.id
    assert retired.display_name == contact.display_name
    assert retired.email_address == contact.email_address
    assert retired.status is ContactStatus.RETIRED
    assert retired.retired_at == NOW + timedelta(hours=1)
    assert retired.created_at == contact.created_at
    assert retired.fingerprint == contact.fingerprint
    assert retired.is_active is False


def test_an_edit_keeps_identity_and_advances_the_meaning() -> None:
    contact = _contact()

    edited = contact.edited(email_address="zhang2@example.edu", at=NOW + timedelta(hours=2))

    assert edited.id == contact.id
    assert edited.created_at == contact.created_at
    assert edited.updated_at == NOW + timedelta(hours=2)
    assert edited.fingerprint != contact.fingerprint
    assert edited.is_active is True


def test_a_retired_contact_cannot_be_edited() -> None:
    retired = _contact().retired(NOW)

    with pytest.raises(InvalidContact):
        retired.edited(display_name="别的名字", at=NOW)


def test_the_lifecycle_flags_must_agree() -> None:
    with pytest.raises(InvalidContact):
        Contact(
            display_name="张老师",
            email_address="zhang@example.edu",
            status=ContactStatus.RETIRED,
            retired_at=None,
            created_at=NOW,
            updated_at=NOW,
        )


def test_timestamps_must_be_aware_and_ordered() -> None:
    with pytest.raises(InvalidContact):
        Contact(
            display_name="张老师",
            email_address="zhang@example.edu",
            created_at=datetime(2026, 9, 21, 9, 0),
            updated_at=NOW,
        )
    with pytest.raises(InvalidContact):
        Contact(
            display_name="张老师",
            email_address="zhang@example.edu",
            created_at=NOW,
            updated_at=NOW - timedelta(minutes=1),
        )
