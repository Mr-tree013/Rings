"""SentMailLookup port: did this exact message reach the Sent mailbox? (ADR-0024).

This is a **read-only** question asked about one already-approved message, and the answer is
deliberately three-valued rather than two:

- `FOUND` — exactly one message in the mailbox carries this exact normalized Message-ID;
- `NOT_FOUND` — nothing matched. That is *not* proof the message was never sent: the mailbox may
  be unavailable locally, the server may file sent mail elsewhere, or the copy may be written
  after this lookup. Nothing may be resent because of it;
- `AMBIGUOUS` — more than one message carries it. Choosing one would be a guess, so the caller
  records the ambiguity and leaves the attempt unresolved.

The port takes the mailbox and the identifier, never a search string: building an IMAP criterion
is the adapter's business, and a caller must not be able to smuggle syntax into a header search.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class SentMailLookupOutcome(StrEnum):
    """What one look in the Sent mailbox concluded."""

    FOUND = "found"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class SentMailMatch:
    """One message whose Message-ID matched exactly."""

    mailbox_name: str
    uidvalidity: int
    uid: int

    def __post_init__(self) -> None:
        if not self.mailbox_name.strip():
            raise ValueError("a sent-mail match needs a mailbox name")
        if self.uidvalidity < 1:
            raise ValueError("uidvalidity must be a positive integer")
        if self.uid < 1:
            raise ValueError("uid must be a positive integer")


@dataclass(frozen=True, slots=True)
class SentMailLookupResult:
    """The outcome plus the matches it was derived from (empty unless `FOUND`)."""

    outcome: SentMailLookupOutcome
    matches: tuple[SentMailMatch, ...] = ()

    def __post_init__(self) -> None:
        if self.outcome is SentMailLookupOutcome.FOUND and len(self.matches) != 1:
            raise ValueError("a FOUND lookup carries exactly one match")
        if self.outcome is SentMailLookupOutcome.NOT_FOUND and self.matches:
            raise ValueError("a NOT_FOUND lookup carries no matches")
        if self.outcome is SentMailLookupOutcome.AMBIGUOUS and len(self.matches) < 2:
            raise ValueError("an AMBIGUOUS lookup carries at least two matches")

    @property
    def match(self) -> SentMailMatch | None:
        """The single match, when there is exactly one."""
        return self.matches[0] if self.outcome is SentMailLookupOutcome.FOUND else None


class SentMailLookup(Protocol):
    """Read-only search of one account's Sent mailbox for one exact Message-ID."""

    async def find_message(
        self, *, mailbox_name: str, rfc_message_id: str
    ) -> SentMailLookupResult:
        """Look for one exact Message-ID in one mailbox.

        Implementations must verify the header themselves rather than trusting a server-side
        `SEARCH` result: a substring or prefix match is not the same message.

        Raises:
            MailCredentialsMissing: no inbound credential is available for this account.
            MailAuthenticationError: the server rejected the credential.
            MailConnectionError: the server could not be reached.
            MailProtocolError: the server answered with something unusable.
        """
        ...


__all__ = [
    "SentMailLookup",
    "SentMailLookupOutcome",
    "SentMailLookupResult",
    "SentMailMatch",
]
