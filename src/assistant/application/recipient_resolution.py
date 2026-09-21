"""Who a letter goes to, and which account sends it — resolved, never guessed (ADR-0037 §10-§14).

```text
explicit address in the human message ─┐
one unambiguous ACTIVE contact        ─┼─► RecipientResolver ─► address
one configured send-ready account     ─┘        │
                                               └─► or MailRecipientUnresolved (a question)
```

This module has no `ModelPort`, no network, no knowledge index and no mail store. Its three inputs
are the *human's own words*, the contact records and the host's configured accounts, which is what
makes "the model cannot invent a recipient" a property of the shape rather than a promise.

The explicit-address rule is the load-bearing one: an address a model proposes is accepted only
when the same normalized address occurs in the raw message the user typed. Everything else — an
unknown name, two contacts with one name, two send-ready accounts — becomes a question.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from assistant.domain.config import MailAccountConfig
from assistant.domain.contact import Contact, normalize_contact_address
from assistant.domain.errors import MailRecipientUnresolved
from assistant.ports.contact_repository import ContactRepository

_ADDRESS_IN_TEXT = re.compile(r"[^\s<>()\[\],;:]+@[^\s<>()\[\],;:]+")
"""A deliberately loose scan: enough to see an address the user typed, never a parser."""


class RecipientSource(StrEnum):
    """Where one resolved recipient address came from."""

    EXPLICIT_EMAIL = "explicit_email"
    CONTACT = "contact"
    SELF = "self"


@dataclass(frozen=True, slots=True)
class ResolvedRecipient:
    """One address, and the deterministic reason it was chosen."""

    address: str
    source: RecipientSource
    contact_id: str | None = None
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedSender:
    """The configured account a new letter would be sent from."""

    account: MailAccountConfig

    @property
    def account_id(self) -> str:
        """The account's stable local id."""
        return self.account.id

    @property
    def from_address(self) -> str:
        """The address this account sends as.

        Raises:
            MailRecipientUnresolved: the account has no usable From address.
        """
        address = normalize_contact_address(self.account.from_address or "")
        if address is None:
            raise MailRecipientUnresolved(
                f"账号 {self.account.id} 没有可用的发件地址"
            )
        return address


def addresses_in_text(text: str) -> tuple[str, ...]:
    """Every address-looking token in one raw message, normalized and de-duplicated.

    This is the whole basis of the explicit-email rule: the runtime asks "did the human write this
    address?", and the answer comes from the human's own text rather than from a model's claim.
    """
    found: list[str] = []
    for match in _ADDRESS_IN_TEXT.findall(text):
        address = normalize_contact_address(match)
        if address is not None and address.lower() not in {item.lower() for item in found}:
            found.append(address)
    return tuple(found)


def text_contains_address(text: str, address: str) -> bool:
    """Whether one normalized address occurs in the raw message, ignoring case."""
    wanted = normalize_contact_address(address)
    if wanted is None:
        return False
    folded = text.casefold()
    if wanted.casefold() in folded:
        return True
    return any(item.casefold() == wanted.casefold() for item in addresses_in_text(text))


def send_ready_accounts(
    accounts: tuple[MailAccountConfig, ...],
) -> tuple[MailAccountConfig, ...]:
    """The configured accounts that could actually send, in configuration order."""
    return tuple(
        account
        for account in accounts
        if account.enabled and account.smtp_configured and account.from_address
    )


def describe_accounts(accounts: tuple[MailAccountConfig, ...]) -> str:
    """A bounded, credential-free list of accounts, for a question back to the user."""
    return "、".join(
        f"{account.id}（{account.from_address or account.username}）" for account in accounts
    )


class RecipientResolver:
    """Turns one of the three allowed recipient sources into an address, or asks."""

    def __init__(
        self,
        contacts: ContactRepository,
        *,
        accounts: tuple[MailAccountConfig, ...] = (),
    ) -> None:
        self._contacts = contacts
        self._accounts = accounts

    @property
    def accounts(self) -> tuple[MailAccountConfig, ...]:
        """Every configured mail account, in configuration order."""
        return self._accounts

    @property
    def send_ready(self) -> tuple[MailAccountConfig, ...]:
        """The configured accounts that could actually send."""
        return send_ready_accounts(self._accounts)

    async def resolve_sender(self, reference: str | None) -> ResolvedSender:
        """Which account a new letter is sent from.

        Raises:
            MailRecipientUnresolved: nothing can send, the named account is unknown, or two
                accounts could be meant. Never resolves by picking the first account.
        """
        candidates = self.send_ready
        if not candidates:
            raise MailRecipientUnresolved(
                "当前没有可发送邮件的邮箱配置，所以我不能发新邮件"
            )
        if reference is None or not reference.strip():
            if len(candidates) == 1:
                return ResolvedSender(account=candidates[0])
            raise MailRecipientUnresolved(
                f"你想从哪个邮箱发送？现在可以发送的是：{describe_accounts(candidates)}"
            )
        matches = _match_accounts(candidates, reference)
        if len(matches) == 1:
            return ResolvedSender(account=matches[0])
        if not matches:
            raise MailRecipientUnresolved(
                f"没有叫「{reference.strip()}」的可发送邮箱；现在可以发送的是："
                f"{describe_accounts(candidates)}"
            )
        raise MailRecipientUnresolved(
            f"「{reference.strip()}」对应多个邮箱，请说清楚用哪一个："
            f"{describe_accounts(matches)}"
        )

    async def resolve_recipient(
        self,
        *,
        kind: RecipientSource,
        address: str | None,
        name: str | None,
        sender: ResolvedSender | None,
    ) -> ResolvedRecipient:
        """The one address this letter goes to.

        Raises:
            MailRecipientUnresolved: the request needs a question rather than a guess.
        """
        if kind is RecipientSource.EXPLICIT_EMAIL:
            return self._explicit(address)
        if kind is RecipientSource.CONTACT:
            return await self._contact(name)
        return self._self(sender)

    # ----------------------------------------------------------------------- internals

    def _explicit(self, address: str | None) -> ResolvedRecipient:
        """An address the model proposed.

        The provenance rule — "the human wrote this address" — is enforced by the conversation
        runtime before the first mutation, from the raw message it alone holds
        (`ConversationService` uses `text_contains_address` for exactly that). What this method
        guarantees is the other half: the address is a usable mailbox, so nothing downstream ever
        has to guess what an unusable value meant.
        """
        if address is None:
            raise MailRecipientUnresolved("我没有看到收件人的邮箱地址")
        normalized = normalize_contact_address(address)
        if normalized is None:
            raise MailRecipientUnresolved(f"{address!r} 不是一个可用的邮箱地址")
        return ResolvedRecipient(address=normalized, source=RecipientSource.EXPLICIT_EMAIL)

    async def _contact(self, name: str | None) -> ResolvedRecipient:
        wanted = (name or "").strip()
        if not wanted:
            raise MailRecipientUnresolved("我没有看到收件人的名字")
        wanted_key = _name_key(wanted)
        matches = [
            contact
            for contact in await self._contacts.list_contacts(status=None)
            if contact.is_active and contact.name_key == wanted_key
        ]
        if not matches:
            raise MailRecipientUnresolved(
                f"我还不知道「{wanted}」的邮箱地址。你可以直接告诉我，例如："
                f"「{wanted}邮箱是 name@example.edu，记成联系人」。"
            )
        if len(matches) > 1:
            listing = "、".join(
                f"{contact.display_name} <{contact.email_address}>" for contact in matches[:5]
            )
            raise MailRecipientUnresolved(
                f"有多个联系人叫「{wanted}」，请告诉我是哪一个：{listing}"
            )
        return _from_contact(matches[0])

    def _self(self, sender: ResolvedSender | None) -> ResolvedRecipient:
        """「我自己」: the configured account's own address, and never the message count."""
        if sender is not None:
            return ResolvedRecipient(
                address=sender.from_address, source=RecipientSource.SELF
            )
        candidates = self.send_ready
        if not candidates:
            raise MailRecipientUnresolved(
                "当前没有可发送邮件的邮箱配置，所以我不能发新邮件"
            )
        if len(candidates) == 1:
            return ResolvedRecipient(
                address=ResolvedSender(account=candidates[0]).from_address,
                source=RecipientSource.SELF,
            )
        raise MailRecipientUnresolved(
            f"你有多个可发送的邮箱，我不确定「我自己」是哪一个：{describe_accounts(candidates)}"
        )


def _from_contact(contact: Contact) -> ResolvedRecipient:
    return ResolvedRecipient(
        address=contact.email_address,
        source=RecipientSource.CONTACT,
        contact_id=str(contact.id),
        display_name=contact.display_name,
    )


def _name_key(name: str) -> str:
    return " ".join(name.split()).casefold()


def _match_accounts(
    accounts: tuple[MailAccountConfig, ...], reference: str
) -> tuple[MailAccountConfig, ...]:
    """Accounts a user-facing reference identifies, exactly and unambiguously.

    An exact match on the account id, its username or its From address wins outright; otherwise the
    reference has to be a substring of exactly one account's id or address, so "school" can select
    an account whose id is `school` but a reference that fits several accounts asks instead.
    """
    wanted = reference.strip().casefold()
    exact = tuple(
        account
        for account in accounts
        if wanted
        in {
            account.id.casefold(),
            account.username.casefold(),
            (account.from_address or "").casefold(),
        }
    )
    if exact:
        return exact
    return tuple(
        account
        for account in accounts
        if wanted in account.id.casefold()
        or wanted in (account.from_address or "").casefold()
    )


__all__ = [
    "RecipientResolver",
    "RecipientSource",
    "ResolvedRecipient",
    "ResolvedSender",
    "addresses_in_text",
    "describe_accounts",
    "send_ready_accounts",
    "text_contains_address",
]
