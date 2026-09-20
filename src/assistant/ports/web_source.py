"""WebSource port: fetch one configured public page, safely (ADR-0029).

The port is narrow on purpose. It takes a target that was *configured*, a URL that was validated and
a byte budget, and it returns either "the content changed and here it is, normalized" or "the
server says it did not change". It cannot be asked to fetch an arbitrary URL by a model, it cannot
follow a redirect, and it never carries cookies, authentication or a browser.

Everything dangerous about talking to the network lives behind this interface: DNS resolution and
the public-address check, TLS, the streaming byte cap, the content-type gate and the
content-addressed snapshot write all happen inside the adapter. The application layer only ever
sees normalized text, a hash and a relative storage key.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from assistant.domain.web_watch import WebTargetId


class WebFetchStatus(StrEnum):
    """The two outcomes a watcher fetch can have."""

    FETCHED = "fetched"
    NOT_MODIFIED = "not-modified"


@dataclass(frozen=True, slots=True)
class WebFetchRequest:
    """One fetch of one configured target."""

    target_id: WebTargetId
    url: str
    max_bytes: int
    timeout_seconds: int
    etag: str | None = None
    last_modified: str | None = None

    @property
    def conditional(self) -> bool:
        """Whether this request carries validators that may earn a `304`."""
        return self.etag is not None or self.last_modified is not None


@dataclass(frozen=True, slots=True)
class WebFetchResult:
    """What one fetch produced. Content fields are absent for `NOT_MODIFIED`."""

    status: WebFetchStatus
    content_type: str | None = None
    normalized_text: str | None = None
    content_sha256: str | None = None
    storage_key: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    bytes_received: int = 0

    @property
    def changed_content(self) -> bool:
        """Whether this result carries new content to compare."""
        return self.status is WebFetchStatus.FETCHED


class WebSource(Protocol):
    """Fetches one configured public page, without following redirects or using cookies."""

    async def fetch(self, request: WebFetchRequest) -> WebFetchResult:
        """Fetch `request.url` and return normalized content or a not-modified answer.

        Raises:
            WebWatchUnsafeAddress: a resolved address is not public.
            WebWatchRedirectNotAllowed: the server answered with a redirect.
            WebWatchUnsupportedContentType: the response is not html, plain text or JSON.
            WebWatchResponseTooLarge: the body exceeded `max_bytes` while streaming.
            WebWatchRequestFailed: a network error, a timeout or an unusable status.
        """
        ...


__all__ = [
    "WebFetchRequest",
    "WebFetchResult",
    "WebFetchStatus",
    "WebSource",
]
