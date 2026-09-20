"""RFC822 parsing into plain data (ADR-0020).

Parsing mail is parsing untrusted input, so the rules are narrow:

- the stdlib `email` package does the MIME work; nothing is rendered, executed or opened;
- headers are decoded (RFC 2047 included) and bounded before they can reach the database;
- the body prefers `text/plain`; an HTML-only message is reduced to text by a small
  `html.parser` subclass that ignores `script`/`style` content and never fetches anything;
- an undecodable charset degrades to replacement characters and is counted as a warning
  instead of wedging one message forever;
- attachment *bytes* become a SHA-256 and metadata. The filename is never a path.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

from assistant.domain.mail import (
    MAX_ADDRESS_CHARS,
    MAX_ADDRESSES,
    MAX_FILENAME_CHARS,
    MAX_SUBJECT_CHARS,
    ParsedAttachment,
    ParsedMail,
    normalize_header_value,
    normalize_message_id,
)

_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "br",
        "li",
        "ul",
        "ol",
        "tr",
        "table",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "section",
        "article",
    }
)
_IGNORED_TAGS = frozenset({"script", "style", "head", "title"})


class _TextExtractingHtmlParser(HTMLParser):
    """Reduce HTML to text: block tags become line breaks, script/style are dropped."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        lowered = tag.lower()
        if lowered in _IGNORED_TAGS:
            self._ignored_depth += 1
        elif lowered in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in _IGNORED_TAGS and self._ignored_depth:
            self._ignored_depth -= 1
        elif lowered in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth and data.strip():
            self._parts.append(data)

    def text(self) -> str:
        """The extracted text, with whitespace collapsed per line."""
        lines = [" ".join(part.split()) for part in "".join(self._parts).splitlines()]
        return "\n".join(line for line in lines if line)


def parse_raw_message(raw: bytes) -> ParsedMail:
    """Parse one raw RFC822 message. Never raises for malformed mail; warns instead."""
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception as exc:
        return ParsedMail(
            warnings=1, warnings_detail=(f"unparsable message: {type(exc).__name__}",)
        )
    return _parse_message(message)


def parse_header_only_message(raw: bytes) -> ParsedMail:
    """Parse headers from a header-only fetch, without looking for a body."""
    parsed = parse_raw_message(raw)
    return ParsedMail(
        message_id_header=parsed.message_id_header,
        in_reply_to_header=parsed.in_reply_to_header,
        references=parsed.references,
        subject=parsed.subject,
        from_address=parsed.from_address,
        to_addresses=parsed.to_addresses,
        cc_addresses=parsed.cc_addresses,
        date_header=parsed.date_header,
        sent_at=parsed.sent_at,
        body_text=None,
        attachments=(),
        warnings=parsed.warnings,
        warnings_detail=parsed.warnings_detail,
    )


def _parse_message(message: Message) -> ParsedMail:
    warnings: list[str] = []
    body_text = _extract_body(message, warnings)
    attachments = _extract_attachments(message, warnings)
    date_header = normalize_header_value(message.get("Date"), limit=MAX_SUBJECT_CHARS)
    return ParsedMail(
        message_id_header=normalize_message_id(message.get("Message-ID")),
        in_reply_to_header=normalize_message_id(message.get("In-Reply-To")),
        references=_references(message),
        subject=normalize_header_value(message.get("Subject"), limit=MAX_SUBJECT_CHARS),
        from_address=_address(message.get("From")),
        to_addresses=_addresses(message.get_all("To", [])),
        cc_addresses=_addresses(message.get_all("Cc", [])),
        date_header=date_header,
        sent_at=_sent_at(date_header, warnings),
        body_text=body_text,
        attachments=attachments,
        warnings=len(warnings),
        warnings_detail=tuple(warnings),
    )


def _references(message: Message) -> tuple[str, ...]:
    references: list[str] = []
    for value in message.get_all("References", []):
        for token in str(value).split():
            normalized = normalize_message_id(token)
            if normalized is not None and normalized not in references:
                references.append(normalized)
    return tuple(references)


def _address(value: object) -> str | None:
    if value is None:
        return None
    return normalize_header_value(str(value), limit=MAX_ADDRESS_CHARS)


def _addresses(values: Sequence[object]) -> tuple[str, ...]:
    addresses: list[str] = []
    for value in values:
        for part in str(value).split(","):
            normalized = normalize_header_value(part, limit=MAX_ADDRESS_CHARS)
            if normalized is not None and normalized not in addresses:
                addresses.append(normalized)
            if len(addresses) == MAX_ADDRESSES:
                return tuple(addresses)
    return tuple(addresses)


def _sent_at(date_header: str | None, warnings: list[str]) -> datetime | None:
    if date_header is None:
        return None
    try:
        parsed = parsedate_to_datetime(date_header)
    except (TypeError, ValueError):
        warnings.append("unparsable Date header")
        return None
    if parsed is None:
        warnings.append("unparsable Date header")
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        # A naive Date header has no trustworthy instant; the raw header is kept instead.
        warnings.append("Date header carries no timezone")
        return None
    return parsed


def _extract_body(message: Message, warnings: list[str]) -> str | None:
    """Prefer non-attachment `text/plain`; fall back to `text/html` reduced to text."""
    plain_parts: list[str] = []
    html_parts: list[str] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        if (part.get_content_disposition() or "").lower() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain":
            text = _decode_text(part, warnings)
            if text is not None:
                plain_parts.append(text)
        elif content_type == "text/html":
            text = _decode_text(part, warnings)
            if text is not None:
                html_parts.append(_html_to_text(text))
    chosen = plain_parts or html_parts
    joined = "\n".join(part.strip() for part in chosen if part.strip())
    return joined or None


def _decode_text(part: Message, warnings: list[str]) -> str | None:
    """Decode one text part, degrading to replacement characters instead of failing."""
    payload = _decoded_payload(part, warnings)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else None
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        warnings.append(f"unknown charset {charset!r}")
        return payload.decode("utf-8", errors="replace")


def _decoded_payload(part: Message, warnings: list[str]) -> bytes | None:
    """The part's decoded bytes, or `None` when it carries no binary payload."""
    try:
        payload = part.get_payload(decode=True)
    except Exception as exc:
        warnings.append(f"undecodable part: {type(exc).__name__}")
        return None
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, bytearray):
        return bytes(payload)
    if isinstance(payload, str):
        return payload.encode("utf-8", errors="replace")
    return None


def _html_to_text(html: str) -> str:
    parser = _TextExtractingHtmlParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return html
    return parser.text()


def _extract_attachments(
    message: Message, warnings: list[str]
) -> tuple[ParsedAttachment, ...]:
    attachments: list[ParsedAttachment] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        if disposition != "attachment" and filename is None:
            continue
        if disposition != "attachment" and part.get_content_type() == "text/plain":
            # An inline text part with a name is the message body, not an attachment.
            continue
        payload = _decoded_payload(part, warnings) or b""
        attachments.append(
            ParsedAttachment(
                ordinal=len(attachments),
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                filename=normalize_header_value(filename, limit=MAX_FILENAME_CHARS),
                content_type=part.get_content_type(),
                content_disposition=(disposition or None),
            )
        )
    return tuple(attachments)


class Rfc822MailParser:
    """The stdlib `email` implementation of the parser port."""

    def parse_full(self, raw: bytes) -> ParsedMail:
        """Parse a complete message."""
        return parse_raw_message(raw)

    def parse_header_only(self, raw: bytes) -> ParsedMail:
        """Parse only the headers of a message fetched without its body."""
        return parse_header_only_message(raw)


__all__ = [
    "Rfc822MailParser",
    "parse_header_only_message",
    "parse_raw_message",
]
