"""Watching a fixed public page, and recording what changed (ADR-0029).

A watcher is a small, deliberately boring thing: one *configured* HTTPS URL, fetched on a timer,
normalized deterministically, hashed, and stored when the hash moves. The model never chooses the
URL, the fetch never follows a redirect, the hostname has to resolve to a public address, and the
response body is capped while it streams. What the phase produces is a durable observation — and
one `InboundEvent` that names it, never the page itself.

The values here are the ones that have to be exact:

- **the target id** (`[a-z][a-z0-9-]{0,62}`) is the stable identity of a configured page, so the
  event source and the state row survive a URL edit;
- **the URL** is checked, not trusted: HTTPS only, no userinfo, no IP-literal host, no explicit
  port. A watcher is a public page, not a generic HTTP client;
- **the normalized text** is what gets hashed, and normalization is line-oriented on purpose: two
  fetches of the same page must produce the same digest, and collapsing everything into one blob
  would destroy the change context an analysis needs;
- **`is_public_ip`** is pure, and it is the rule the transport adapter applies to *every* address a
  hostname resolves to. Loopback, private, link-local, multicast, reserved and unspecified
  addresses are all refused.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from assistant.domain.errors import (
    InvalidWebTarget,
    WebWatchUnsafeUrl,
)

WebTargetId = str
"""Stable identity of one configured watcher target."""

WebObservationId = UUID
"""Stable identity of one recorded observation."""

TARGET_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
"""Lowercase, hyphenated, short: an id that reads the same in a config file and an event source."""

MAX_NORMALIZED_CHARS = 400_000
"""How much normalized text one observation may hold, so one page cannot fill the disk."""


def new_web_observation_id() -> WebObservationId:
    """Generate a fresh observation identity."""
    return uuid4()


def validate_target_id(value: str) -> WebTargetId:
    """Return the trimmed target id, or raise `InvalidWebTarget`."""
    if not isinstance(value, str):
        raise InvalidWebTarget("a watcher target id must be text")
    stripped = value.strip()
    if not TARGET_ID_PATTERN.match(stripped):
        raise InvalidWebTarget(
            f"a watcher target id must match {TARGET_ID_PATTERN.pattern} (got {value!r})"
        )
    return stripped


def validate_watch_url(url: str) -> str:
    """Return the configured HTTPS URL, or explain why it may not be watched.

    Raises:
        WebWatchUnsafeUrl: the URL is not an HTTPS page URL without credentials, an IP-literal
            host or an explicit port.
    """
    if not isinstance(url, str):
        raise WebWatchUnsafeUrl(url, "a watcher URL must be text")
    stripped = url.strip()
    if not stripped:
        raise WebWatchUnsafeUrl(url, "a watcher URL must not be blank")
    parts = urlsplit(stripped)
    if parts.scheme != "https":
        raise WebWatchUnsafeUrl(stripped, "only https URLs can be watched")
    if not parts.hostname:
        raise WebWatchUnsafeUrl(stripped, "a watcher URL needs a hostname")
    # Any userinfo at all is refused. `username` being present (even empty) is exactly the signal
    # that a credential was written into the URL, and this module never has to look at the rest.
    if parts.username is not None:
        raise WebWatchUnsafeUrl(stripped, "credentials in a URL are never used or stored")
    try:
        ipaddress.ip_address(parts.hostname)
    except ValueError:
        pass
    else:
        raise WebWatchUnsafeUrl(stripped, "an IP-literal host is not a public page")
    if parts.port is not None:
        raise WebWatchUnsafeUrl(
            stripped, "only the default HTTPS port is watched in this phase"
        )
    # A fragment never reaches a server, so it is dropped rather than carried around.
    return stripped.split("#", 1)[0]


def is_public_ip(address: str) -> bool:
    """Whether one resolved address is a public one this project will connect to.

    The positive test is `is_global`, and the explicit list below documents what that excludes:
    loopback, private, link-local, multicast, reserved and unspecified space — plus ranges such as
    the carrier-grade NAT block that are routable in principle but never the public page a watcher
    is configured for.
    """
    try:
        parsed = ipaddress.ip_address(address.strip())
    except ValueError:
        return False
    if not parsed.is_global:
        return False
    return not (
        parsed.is_loopback
        or parsed.is_private
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_reserved
        or parsed.is_unspecified
    )


class WebContentType(StrEnum):
    """The three response types this phase understands. Everything else is a refusal."""

    HTML = "text/html"
    PLAIN = "text/plain"
    JSON = "application/json"

    @classmethod
    def parse(cls, header: str | None) -> WebContentType | None:
        """Return the supported type of a `Content-Type` header, or `None`.

        Parameters are ignored (`text/html; charset=utf-8` is HTML), comparison is
        case-insensitive, and anything else — a PDF, an image, a zip — has no value here.
        """
        if not header:
            return None
        media_type = header.split(";", 1)[0].strip().lower()
        for member in cls:
            if member.value == media_type:
                return member
        return None


def normalize_text(text: str) -> str:
    """Normalize extracted text so that two readings of the same page hash equally.

    CRLF becomes LF, trailing whitespace on each line is removed, leading and trailing blank lines
    are dropped, and runs of more than two blank lines collapse to two. Line structure survives,
    because the change context is built from it.
    """
    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in unified.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    collapsed: list[str] = []
    blank_run = 0
    for line in lines:
        if line.strip():
            blank_run = 0
            collapsed.append(line)
            continue
        blank_run += 1
        if blank_run <= 2:
            collapsed.append("")
    normalized = "\n".join(collapsed)
    if len(normalized) > MAX_NORMALIZED_CHARS:
        normalized = normalized[:MAX_NORMALIZED_CHARS]
    return normalized


def content_sha256(normalized_text: str) -> str:
    """The content identity of one observation: SHA-256 of the normalized UTF-8 text."""
    return hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class WebObservation:
    """One recorded version of a watched page."""

    target_id: WebTargetId
    url: str
    content_sha256: str
    storage_key: str
    fetched_at: datetime
    id: WebObservationId = field(default_factory=new_web_observation_id)
    previous_observation_id: WebObservationId | None = None
    is_baseline: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_id", validate_target_id(self.target_id))
        if len(self.content_sha256) != 64:
            raise InvalidWebTarget("a web observation needs a SHA-256 content hash")
        if not self.storage_key.strip():
            raise InvalidWebTarget("a web observation needs a snapshot storage key")
        if self.fetched_at.tzinfo is None or self.fetched_at.utcoffset() is None:
            raise InvalidWebTarget("fetched_at must be timezone-aware")
        if self.is_baseline and self.previous_observation_id is not None:
            raise InvalidWebTarget("a baseline observation replaces nothing")
        if not self.is_baseline and self.previous_observation_id is None:
            raise InvalidWebTarget("a change observation names the observation it replaced")


@dataclass(frozen=True, slots=True)
class WebWatchState:
    """What is known about one configured target between polls."""

    target_id: WebTargetId
    url: str
    updated_at: datetime
    content_sha256: str | None = None
    latest_observation_id: WebObservationId | None = None
    etag: str | None = None
    last_modified: str | None = None
    checks_since_full: int = 0
    last_checked_at: datetime | None = None
    last_changed_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_id", validate_target_id(self.target_id))
        if self.checks_since_full < 0:
            raise InvalidWebTarget("checks_since_full must not be negative")
        for name in ("updated_at", "last_checked_at", "last_changed_at"):
            value = getattr(self, name)
            if value is None:
                continue
            if value.tzinfo is None or value.utcoffset() is None:
                raise InvalidWebTarget(f"{name} must be timezone-aware")

    @property
    def has_baseline(self) -> bool:
        """Whether this target has ever been fetched successfully."""
        return self.content_sha256 is not None

    def conditional_headers(
        self, *, full_fetch_every: int, reset: bool
    ) -> tuple[str | None, str | None] | None:
        """The validators to send, or `None` when the next fetch must be unconditional.

        A periodic full fetch is what keeps the optimization honest: a server that answers `304`
        forever cannot hide a change, because every `full_fetch_every` checks the content itself.
        """
        if reset or not self.has_baseline:
            return None
        if self.checks_since_full >= full_fetch_every:
            return None
        if self.etag is None and self.last_modified is None:
            return None
        return (self.etag, self.last_modified)


__all__ = [
    "MAX_NORMALIZED_CHARS",
    "TARGET_ID_PATTERN",
    "WebContentType",
    "WebObservation",
    "WebObservationId",
    "WebTargetId",
    "WebWatchState",
    "content_sha256",
    "is_public_ip",
    "new_web_observation_id",
    "normalize_text",
    "validate_target_id",
    "validate_watch_url",
]
