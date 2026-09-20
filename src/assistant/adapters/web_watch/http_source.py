"""The HTTPS implementation of `WebSource` (ADR-0029).

Everything that could go wrong with talking to the network is handled here, in one place, and the
choices are all conservative:

- **the hostname is resolved first and every address is checked.** A name that resolves to
  loopback, a private range, link-local, multicast, reserved or unspecified space is refused with
  `WebWatchUnsafeAddress`, whatever it *looks* like as text;
- **redirects are not followed.** A `3xx` is an error the user fixes by configuring the final URL,
  which is what keeps a watcher from being steered to another origin;
- **the body is read in chunks and capped.** Exceeding `max_bytes` aborts the fetch and the
  snapshot is never written, so an endless response cannot fill the disk or move a baseline;
- **there are no cookies, no authentication, no `Authorization` header and no browser.** The
  environment is not trusted either (`trust_env=False` in composition), so no ambient proxy or CA
  setting can redirect a watcher somewhere else.

What leaves this module is normalized text, its SHA-256 and a relative storage key. The page itself
stays on disk in the content-addressed store.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Callable, Sequence
from urllib.parse import urlsplit

import httpx

from assistant.adapters.web_watch.extractor import extract_text
from assistant.adapters.web_watch.snapshot_store import WebSnapshotStore
from assistant.domain.errors import (
    WebWatchRedirectNotAllowed,
    WebWatchRequestFailed,
    WebWatchResponseTooLarge,
    WebWatchUnsafeAddress,
    WebWatchUnsupportedContentType,
)
from assistant.domain.web_watch import WebContentType, is_public_ip
from assistant.ports.web_source import (
    WebFetchRequest,
    WebFetchResult,
    WebFetchStatus,
)

USER_AGENT = "growing-assistant-watcher/1"
"""A fixed agent string: this is a program fetching a public page, and it says so."""

MAX_REDIRECT_STATUS = 400


def default_resolver(host: str) -> Sequence[str]:
    """Resolve a hostname to every address it currently maps to.

    Blocking on purpose: the caller runs it in a worker thread, and resolution is the one thing a
    watcher cannot avoid doing synchronously before it decides whether it is allowed to connect.
    """
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise WebWatchRequestFailed(
            host, f"the hostname could not be resolved ({type(exc).__name__})"
        ) from exc
    return tuple(str(info[4][0]) for info in infos)


class HttpWebSource:
    """Fetches one configured HTTPS page, with no cookies, no redirects and a byte budget."""

    def __init__(
        self,
        client_factory: Callable[[], httpx.AsyncClient],
        snapshots: WebSnapshotStore,
        *,
        resolver: Callable[[str], Sequence[str]] | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._snapshots = snapshots
        self._resolver = resolver if resolver is not None else default_resolver

    async def fetch(self, request: WebFetchRequest) -> WebFetchResult:
        """Fetch one configured target, or explain why it could not be fetched."""
        host = urlsplit(request.url).hostname or ""
        await self._require_public_host(request.url, host)
        headers = {"Accept": "text/html, text/plain, application/json", "User-Agent": USER_AGENT}
        if request.etag:
            headers["If-None-Match"] = request.etag
        if request.last_modified:
            headers["If-Modified-Since"] = request.last_modified
        try:
            async with self._client_factory() as client, client.stream(
                "GET",
                request.url,
                headers=headers,
                timeout=float(request.timeout_seconds),
                follow_redirects=False,
            ) as response:
                return await self._handle_response(response, request)
        except httpx.HTTPError as exc:
            raise WebWatchRequestFailed(
                request.url, f"{type(exc).__name__}: {exc}"[:200]
            ) from exc

    async def _require_public_host(self, url: str, host: str) -> None:
        """Resolve the hostname and refuse anything that is not a public address."""
        if not host:
            raise WebWatchRequestFailed(url, "the URL has no hostname")
        addresses = await asyncio.to_thread(self._resolver, host)
        if not addresses:
            raise WebWatchRequestFailed(url, "the hostname resolved to no addresses")
        for address in addresses:
            if not is_public_ip(address):
                raise WebWatchUnsafeAddress(
                    host, address, "not a public address, so it will not be contacted"
                )

    async def _handle_response(
        self, response: httpx.Response, request: WebFetchRequest
    ) -> WebFetchResult:
        status = response.status_code
        etag = response.headers.get("etag")
        last_modified = response.headers.get("last-modified")
        if status == 304:
            # `304` is the cache validation answer, not a redirect: it is the one 3xx this adapter
            # is allowed to accept, and it carries no body by definition.
            return WebFetchResult(
                status=WebFetchStatus.NOT_MODIFIED,
                etag=request.etag if etag is None else etag,
                last_modified=(
                    request.last_modified if last_modified is None else last_modified
                ),
            )
        if 300 <= status < MAX_REDIRECT_STATUS:
            raise WebWatchRedirectNotAllowed(
                request.url, status, response.headers.get("location")
            )
        if status >= MAX_REDIRECT_STATUS:
            raise WebWatchRequestFailed(request.url, f"the server answered HTTP {status}")
        header = response.headers.get("content-type")
        content_type = WebContentType.parse(header)
        if content_type is None:
            raise WebWatchUnsupportedContentType(header)
        raw = await self._read_bounded(response, request)
        normalized = extract_text(content_type, raw)
        digest, storage_key = await self._snapshots.store_text(normalized)
        return WebFetchResult(
            status=WebFetchStatus.FETCHED,
            content_type=content_type.value,
            normalized_text=normalized,
            content_sha256=digest,
            storage_key=storage_key,
            etag=etag,
            last_modified=last_modified,
            bytes_received=len(raw),
        )

    async def _read_bounded(
        self, response: httpx.Response, request: WebFetchRequest
    ) -> bytes:
        """Read the body in chunks, aborting the moment it exceeds the budget."""
        buffer = bytearray()
        async for chunk in response.aiter_bytes():
            buffer.extend(chunk)
            if len(buffer) > request.max_bytes:
                raise WebWatchResponseTooLarge(request.url, request.max_bytes)
        return bytes(buffer)


__all__ = ["USER_AGENT", "HttpWebSource", "default_resolver"]
