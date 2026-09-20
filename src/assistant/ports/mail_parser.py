"""MailParser port: raw RFC822 bytes in, parsed values out (ADR-0020).

Parsing is a distinct responsibility from talking IMAP and from storing results, so it gets its
own small port: the application asks for parsed values and never learns that the `email` package
exists, and a test can script malformed input without a mailbox.
"""

from __future__ import annotations

from typing import Protocol

from assistant.domain.mail import ParsedMail


class MailParser(Protocol):
    """Turns RFC822 bytes into the values this project stores."""

    def parse_full(self, raw: bytes) -> ParsedMail:
        """Parse a complete message, body and attachments included.

        Implementations must never raise for malformed mail: they degrade, count warnings and
        return what they could read.
        """
        ...

    def parse_header_only(self, raw: bytes) -> ParsedMail:
        """Parse only the headers of a message that was fetched without its body."""
        ...


__all__ = ["MailParser"]
