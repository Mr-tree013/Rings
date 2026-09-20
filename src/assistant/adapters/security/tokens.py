"""Cryptographic token generation (ADR-0023).

This module, and the RFC Message-ID factory, are the only places the project reaches for a
system randomness source. It lives in
`adapters/` on purpose: the domain and the application must not be able to *decide* how a secret
is made, they receive a `Callable[[], str]` and use whatever it returns. That keeps the token
policy reviewable in one file, and it lets every test inject a deterministic stand-in.

`secrets.token_urlsafe(32)` yields 256 bits of entropy from the operating system's CSPRNG, in a
form that survives being pasted into a terminal.
"""

from __future__ import annotations

import secrets

TOKEN_BYTES = 32
"""256 bits. The floor for a value that stands in for a human decision."""


def secure_approval_token_factory() -> str:
    """Return a fresh, URL-safe, 256-bit approval token."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def secure_mobile_token_factory() -> str:
    """Return a fresh, URL-safe, 256-bit token for a pairing code, session or CSRF companion.

    The same generator as the approval token, by design: one reviewed random source, one length,
    one encoding.
    """
    return secrets.token_urlsafe(TOKEN_BYTES)


__all__ = [
    "TOKEN_BYTES",
    "secure_approval_token_factory",
    "secure_mobile_token_factory",
]
