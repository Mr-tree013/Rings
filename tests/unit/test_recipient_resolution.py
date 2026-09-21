"""Deterministic recipient and sender resolution (ADR-0037 §10-§14).

No model, no network, no mail store: the resolver's whole input is the human's words, the stored
contacts and the configured accounts. Every "it might mean" case is asserted to produce a question
instead of a choice.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from assistant.application.recipient_resolution import (
    RecipientResolver,
    RecipientSource,
    addresses_in_text,
    send_ready_accounts,
    text_contains_address,
)
from assistant.domain.config import MailAccountConfig
from assistant.domain.contact import Contact
from assistant.domain.errors import MailRecipientUnresolved
from assistant.store.contacts import SqliteContactRepository
from assistant.store.db import Database
from assistant.store.migrations import apply_migrations
from tests.support.fakes import FakeClock
from tests.support.mail_send import SEND_ACCOUNT_ID, smtp_account

NOW = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)


def _second_account() -> MailAccountConfig:
    return MailAccountConfig(
        id="school",
        host="imap.example.edu",
        username="school@example.edu",
        mailbox="INBOX",
        enabled=True,
        smtp_host="smtp.example.edu",
        smtp_username="school@example.edu",
        from_address="school@example.edu",
    )


def _receive_only() -> MailAccountConfig:
    return MailAccountConfig(
        id="readonly",
        host="imap.example.edu",
        username="readonly@example.edu",
        mailbox="INBOX",
        enabled=True,
    )


@pytest.fixture
def database(tmp_path) -> Database:
    database = Database.at(tmp_path / "assistant.db")
    apply_migrations(database, clock=FakeClock(start=NOW))
    return database


async def _resolver(
    database: Database, accounts: tuple[MailAccountConfig, ...]
) -> RecipientResolver:
    return RecipientResolver(
        SqliteContactRepository(database), accounts=accounts
    )


async def _store_contact(
    database: Database,
    *,
    name: str = "张老师",
    address: str = "zhang@example.edu",
) -> Contact:
    contact = Contact(
        display_name=name,
        email_address=address,
        created_at=NOW,
        updated_at=NOW,
    )
    return await SqliteContactRepository(database).add_contact(contact)


# ---------------------------------------------------------------- address extraction


def test_addresses_are_read_from_the_humans_own_words() -> None:
    text = "给 Alice@Example.com 发封邮件，抄送 <bob@example.edu>。"

    assert addresses_in_text(text) == ("Alice@Example.com", "bob@example.edu")
    assert text_contains_address(text, "alice@example.com") is True
    assert text_contains_address(text, "bob@example.edu") is True
    assert text_contains_address(text, "carol@example.edu") is False


def test_a_non_address_never_matches() -> None:
    assert addresses_in_text("没有地址") == ()
    assert text_contains_address("没有地址", "someone@example.edu") is False


# ------------------------------------------------------------------- own accounts


def test_only_send_ready_accounts_can_send() -> None:
    accounts = (smtp_account(), _second_account(), _receive_only())

    ready = send_ready_accounts(accounts)

    assert [account.id for account in ready] == [SEND_ACCOUNT_ID, "school"]


async def test_one_send_ready_account_is_used_without_asking(database: Database) -> None:
    resolver = await _resolver(database, (smtp_account(), _receive_only()))

    sender = await resolver.resolve_sender(None)

    assert sender.account_id == SEND_ACCOUNT_ID
    assert sender.from_address == "student@example.edu"


async def test_two_send_ready_accounts_ask(database: Database) -> None:
    resolver = await _resolver(database, (smtp_account(), _second_account()))

    with pytest.raises(MailRecipientUnresolved) as failure:
        await resolver.resolve_sender(None)

    assert "哪个邮箱" in str(failure.value)


async def test_a_named_account_must_be_identifiable(database: Database) -> None:
    resolver = await _resolver(database, (smtp_account(), _second_account()))

    assert (await resolver.resolve_sender("school")).account_id == "school"
    assert (await resolver.resolve_sender("school@example.edu")).account_id == "school"
    with pytest.raises(MailRecipientUnresolved):
        await resolver.resolve_sender("nowhere")


async def test_no_send_ready_account_says_so(database: Database) -> None:
    resolver = await _resolver(database, (_receive_only(),))

    with pytest.raises(MailRecipientUnresolved) as failure:
        await resolver.resolve_sender(None)

    assert "没有可发送邮件的邮箱配置" in str(failure.value)


# --------------------------------------------------------------------- recipients


async def test_an_explicit_address_is_taken_from_the_operation(
    database: Database,
) -> None:
    resolver = await _resolver(database, (smtp_account(),))
    sender = await resolver.resolve_sender(None)

    recipient = await resolver.resolve_recipient(
        kind=RecipientSource.EXPLICIT_EMAIL,
        address="Alice@Example.com",
        name=None,
        sender=sender,
    )

    assert recipient.address == "Alice@Example.com"
    assert recipient.source is RecipientSource.EXPLICIT_EMAIL


async def test_an_unusable_explicit_address_is_refused(database: Database) -> None:
    resolver = await _resolver(database, (smtp_account(),))

    with pytest.raises(MailRecipientUnresolved):
        await resolver.resolve_recipient(
            kind=RecipientSource.EXPLICIT_EMAIL,
            address="not an address",
            name=None,
            sender=None,
        )


async def test_one_contact_resolves_an_address(database: Database) -> None:
    contact = await _store_contact(database)
    resolver = await _resolver(database, (smtp_account(),))

    recipient = await resolver.resolve_recipient(
        kind=RecipientSource.CONTACT, address=None, name="张老师", sender=None
    )

    assert recipient.address == "zhang@example.edu"
    assert recipient.contact_id == str(contact.id)
    assert recipient.source is RecipientSource.CONTACT


async def test_an_unknown_name_asks_for_the_address(database: Database) -> None:
    resolver = await _resolver(database, (smtp_account(),))

    with pytest.raises(MailRecipientUnresolved) as failure:
        await resolver.resolve_recipient(
            kind=RecipientSource.CONTACT, address=None, name="李老师", sender=None
        )

    assert "我还不知道「李老师」的邮箱地址" in str(failure.value)
    assert "记成联系人" in str(failure.value)


async def test_two_contacts_with_one_name_ask_which(database: Database) -> None:
    await _store_contact(database)
    await _store_contact(database, address="zhang2@example.edu")
    resolver = await _resolver(database, (smtp_account(),))

    with pytest.raises(MailRecipientUnresolved) as failure:
        await resolver.resolve_recipient(
            kind=RecipientSource.CONTACT, address=None, name="张老师", sender=None
        )

    message = str(failure.value)
    assert "多个联系人" in message
    assert "zhang@example.edu" in message and "zhang2@example.edu" in message


async def test_a_retired_contact_does_not_resolve(database: Database) -> None:
    contact = await _store_contact(database)
    repository = SqliteContactRepository(database)
    await repository.update_contact(contact.retired(NOW))
    resolver = await _resolver(database, (smtp_account(),))

    with pytest.raises(MailRecipientUnresolved):
        await resolver.resolve_recipient(
            kind=RecipientSource.CONTACT, address=None, name="张老师", sender=None
        )


async def test_self_uses_the_senders_own_address(database: Database) -> None:
    resolver = await _resolver(database, (smtp_account(), _second_account()))
    sender = await resolver.resolve_sender("school")

    recipient = await resolver.resolve_recipient(
        kind=RecipientSource.SELF, address=None, name=None, sender=sender
    )

    assert recipient.address == "school@example.edu"
    assert recipient.source is RecipientSource.SELF


async def test_self_without_a_named_account_asks_when_two_could_send(
    database: Database,
) -> None:
    resolver = await _resolver(database, (smtp_account(), _second_account()))

    with pytest.raises(MailRecipientUnresolved):
        await resolver.resolve_recipient(
            kind=RecipientSource.SELF, address=None, name=None, sender=None
        )


async def test_self_with_one_account_is_that_account(database: Database) -> None:
    resolver = await _resolver(database, (smtp_account(),))

    recipient = await resolver.resolve_recipient(
        kind=RecipientSource.SELF, address=None, name=None, sender=None
    )

    assert recipient.address == "student@example.edu"
