"""RFC 5322 Message-ID generation (ADR-0024).

This is the second and last module in the project that reaches for a system randomness source
(the first mints approval tokens). It lives in `adapters/` for the same reason: the domain and the
application must not be able to *decide* how a globally unique identifier is made — they receive a
factory and use what it returns, which also lets every test inject a deterministic value.

The id is generated **before approval** and becomes part of the approved payload. That is what
makes "did this exact message arrive?" answerable later: the Sent-folder lookup searches for this
string, not for something reconstructed at reconciliation time.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable

MESSAGE_ID_RANDOM_BYTES = 16
"""128 bits of randomness, the floor the ADR asks for."""


def new_rfc_message_id(domain: str) -> str:
    """Return a fresh `<random@domain>` identifier for one outbound message.

    The domain half is the sender's own domain, which is what makes the id look like ordinary mail
    to the receiving system instead of a local convention it might rewrite.
    """
    cleaned = domain.strip().strip("<>.")
    if not cleaned or any(character.isspace() for character in cleaned):
        raise ValueError(f"a Message-ID needs a usable domain, not {domain!r}")
    return f"<{secrets.token_hex(MESSAGE_ID_RANDOM_BYTES)}@{cleaned}>"


def secure_rfc_message_id_factory() -> Callable[[str], str]:
    """The production factory: given a sender domain, mint a random Message-ID."""
    return new_rfc_message_id


__all__ = [
    "MESSAGE_ID_RANDOM_BYTES",
    "new_rfc_message_id",
    "secure_rfc_message_id_factory",
]
