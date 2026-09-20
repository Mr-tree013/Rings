"""The HTTPS `WebSource` against a strict mock transport (ADR-0029).

No socket is opened: `httpx.MockTransport` answers every request, and the resolver is a fake, so
these tests are about the *policy* rather than about the network. What is under test is everything
a server could do to a watcher — redirect it, answer with a PDF, stream forever, time out, fail —
and everything the adapter must refuse before it even connects.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest

from assistant.adapters.web_watch.http_source import HttpWebSource
from assistant.adapters.web_watch.snapshot_store import WebSnapshotStore
from assistant.domain.errors import (
    WebWatchRedirectNotAllowed,
    WebWatchRequestFailed,
    WebWatchResponseTooLarge,
    WebWatchUnsafeAddress,
    WebWatchUnsupportedContentType,
)
from assistant.ports.web_source import WebFetchRequest, WebFetchStatus

URL = "https://example.edu/notices"
PUBLIC = "93.184.216.34"


def _request(**overrides: object) -> WebFetchRequest:
    values: dict[str, object] = {
        "target_id": "course-notices",
        "url": URL,
        "max_bytes": 4096,
        "timeout_seconds": 5,
    }
    values.update(overrides)
    return WebFetchRequest(**values)  # type: ignore[arg-type]


def _source(
    handler,
    tmp_path: Path,
    *,
    addresses: Sequence[str] = (PUBLIC,),
    seen: list[httpx.Request] | None = None,
) -> HttpWebSource:
    """A real adapter over a mock transport and a scripted resolver."""
    recorded = seen if seen is not None else []

    def wrapped(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return handler(request)

    return HttpWebSource(
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(wrapped)),
        WebSnapshotStore(tmp_path),
        resolver=lambda host: addresses,
    )


def _ok(body: bytes, content_type: str = "text/html", **headers: str):
    return lambda request: httpx.Response(
        200, content=body, headers={"content-type": content_type, **headers}
    )


# ------------------------------------------------------------------------ happy paths


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("text/html", b"<p>Registration closes Oct 20.</p>"),
        ("text/plain", b"Registration closes Oct 20."),
        ("application/json", b'{"closes": "2026-10-20"}'),
    ],
)
async def test_a_supported_response_is_normalized_hashed_and_stored(
    tmp_path: Path, content_type: str, body: bytes
) -> None:
    source = _source(_ok(body, content_type), tmp_path)

    result = await source.fetch(_request())

    assert result.status is WebFetchStatus.FETCHED
    assert result.content_type == content_type
    assert result.normalized_text is not None
    assert result.content_sha256 is not None
    assert result.storage_key is not None
    assert (tmp_path / result.storage_key).read_text(encoding="utf-8") == result.normalized_text
    assert result.bytes_received == len(body)


async def test_the_request_carries_no_credentials_and_asks_for_no_redirects(
    tmp_path: Path,
) -> None:
    seen: list[httpx.Request] = []
    source = _source(_ok(b"<p>hello</p>"), tmp_path, seen=seen)

    await source.fetch(_request())

    request = seen[0]
    assert request.headers.get("authorization") is None
    assert request.headers.get("cookie") is None
    assert "growing-assistant-watcher" in request.headers["user-agent"]


async def test_conditional_validators_are_sent_when_asked(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    source = _source(_ok(b"<p>hello</p>"), tmp_path, seen=seen)

    await source.fetch(_request(etag='W/"7"', last_modified="Wed, 24 Sep 2026 09:00:00 GMT"))

    assert seen[0].headers["if-none-match"] == 'W/"7"'
    assert seen[0].headers["if-modified-since"] == "Wed, 24 Sep 2026 09:00:00 GMT"


async def test_a_full_fetch_sends_no_validators(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    source = _source(_ok(b"<p>hello</p>"), tmp_path, seen=seen)

    await source.fetch(_request())

    assert "if-none-match" not in seen[0].headers
    assert "if-modified-since" not in seen[0].headers


async def test_a_not_modified_answer_returns_no_content(tmp_path: Path) -> None:
    source = _source(lambda request: httpx.Response(304), tmp_path)

    result = await source.fetch(_request(etag='W/"7"'))

    assert result.status is WebFetchStatus.NOT_MODIFIED
    assert result.normalized_text is None
    assert result.content_sha256 is None
    assert result.etag == 'W/"7"'  # the validator is kept when the server repeats nothing
    assert list(tmp_path.rglob("*.txt")) == []


async def test_a_successful_response_refreshes_its_validators(tmp_path: Path) -> None:
    source = _source(
        _ok(b"<p>hello</p>", etag='W/"9"', **{"last-modified": "Thu, 25 Sep 2026 09:00:00 GMT"}),
        tmp_path,
    )

    result = await source.fetch(_request())

    assert result.etag == 'W/"9"'
    assert result.last_modified == "Thu, 25 Sep 2026 09:00:00 GMT"


# --------------------------------------------------------------------------- refusals


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_a_redirect_is_refused_and_not_followed(
    tmp_path: Path, status: int
) -> None:
    seen: list[httpx.Request] = []
    source = _source(
        lambda request: httpx.Response(
            status, headers={"location": "https://elsewhere.example/x"}
        ),
        tmp_path,
        seen=seen,
    )

    with pytest.raises(WebWatchRedirectNotAllowed) as raised:
        await source.fetch(_request())

    assert len(seen) == 1  # nothing was requested from the redirect target
    assert raised.value.status == status
    assert raised.value.location == "https://elsewhere.example/x"


@pytest.mark.parametrize("status", [400, 403, 404, 500, 502, 503])
async def test_an_unusable_status_is_refused(tmp_path: Path, status: int) -> None:
    source = _source(lambda request: httpx.Response(status), tmp_path)

    with pytest.raises(WebWatchRequestFailed) as raised:
        await source.fetch(_request())

    assert f"HTTP {status}" in raised.value.reason


@pytest.mark.parametrize(
    "content_type",
    ["application/pdf", "image/png", "application/zip", "text/csv", "application/octet-stream"],
)
async def test_an_unsupported_content_type_is_refused(
    tmp_path: Path, content_type: str
) -> None:
    source = _source(_ok(b"%PDF-1.7", content_type), tmp_path)

    with pytest.raises(WebWatchUnsupportedContentType):
        await source.fetch(_request())

    assert list(tmp_path.rglob("*.txt")) == []  # nothing was stored


async def test_an_oversize_response_is_abandoned_without_a_snapshot(tmp_path: Path) -> None:
    """§11: the cap is enforced while streaming, and the baseline hash never moves."""
    body = b"x" * 10_000
    source = _source(_ok(body), tmp_path)

    with pytest.raises(WebWatchResponseTooLarge):
        await source.fetch(_request(max_bytes=1024))

    assert list(tmp_path.rglob("*.txt")) == []


async def test_a_timeout_is_an_operational_failure(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    source = _source(handler, tmp_path)

    with pytest.raises(WebWatchRequestFailed) as raised:
        await source.fetch(_request())

    assert "ReadTimeout" in raised.value.reason


async def test_a_network_failure_is_an_operational_failure(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    source = _source(handler, tmp_path)

    with pytest.raises(WebWatchRequestFailed) as raised:
        await source.fetch(_request())

    assert "ConnectError" in raised.value.reason


# ------------------------------------------------------------------------ addresses


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.1.1",
        "::1",
        "fc00::1",
        "0.0.0.0",
    ],
)
async def test_a_private_resolution_is_refused_before_any_connection(
    tmp_path: Path, address: str
) -> None:
    seen: list[httpx.Request] = []
    source = _source(_ok(b"<p>hello</p>"), tmp_path, addresses=(address,), seen=seen)

    with pytest.raises(WebWatchUnsafeAddress) as raised:
        await source.fetch(_request())

    assert raised.value.address == address
    assert seen == []  # the transport was never asked for anything


async def test_one_unsafe_address_among_several_is_enough_to_refuse(
    tmp_path: Path,
) -> None:
    seen: list[httpx.Request] = []
    source = _source(
        _ok(b"<p>hello</p>"), tmp_path, addresses=(PUBLIC, "127.0.0.1"), seen=seen
    )

    with pytest.raises(WebWatchUnsafeAddress):
        await source.fetch(_request())

    assert seen == []


async def test_a_hostname_that_resolves_to_nothing_is_refused(tmp_path: Path) -> None:
    source = _source(_ok(b"<p>hello</p>"), tmp_path, addresses=())

    with pytest.raises(WebWatchRequestFailed):
        await source.fetch(_request())


async def test_a_resolver_failure_is_an_operational_failure(tmp_path: Path) -> None:
    def resolver(host: str) -> Sequence[str]:
        raise WebWatchRequestFailed(host, "the hostname could not be resolved")

    source = HttpWebSource(
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_ok(b"x"))),
        WebSnapshotStore(tmp_path),
        resolver=resolver,
    )

    with pytest.raises(WebWatchRequestFailed):
        await source.fetch(_request())
