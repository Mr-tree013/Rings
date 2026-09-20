"""Watcher values: URLs, addresses, normalization, observations and state (ADR-0029).

The rules under test are the ones that hold before a socket is opened: a watcher URL is an HTTPS
page URL and nothing else, an address is contacted only if it is public, the same page always
normalizes to the same text, and a target's state knows when it must stop trusting the server's
validators.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from assistant.domain.errors import InvalidWebTarget, WebWatchUnsafeUrl
from assistant.domain.web_watch import (
    MAX_NORMALIZED_CHARS,
    WebContentType,
    WebObservation,
    WebWatchState,
    content_sha256,
    is_public_ip,
    new_web_observation_id,
    normalize_text,
    validate_target_id,
    validate_watch_url,
)

NOW = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- target ids


@pytest.mark.parametrize("value", ["a", "course-notices", "x" * 63, "exam2026"])
def test_a_stable_target_id_is_accepted(value: str) -> None:
    assert validate_target_id(value) == value


@pytest.mark.parametrize(
    "value", ["", " ", "Course", "1course", "-course", "course_notices", "x" * 64, "a.b"]
)
def test_an_unstable_target_id_is_refused(value: str) -> None:
    with pytest.raises(InvalidWebTarget):
        validate_target_id(value)


# ------------------------------------------------------------------------- watch urls


@pytest.mark.parametrize(
    "url",
    [
        "https://example.edu/notices",
        "https://example.edu",
        "https://example.edu/a/b?q=1",
        "https://sub.domain.example.edu/x",
    ],
)
def test_a_public_https_url_is_accepted(url: str) -> None:
    assert validate_watch_url(url) == url


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://example.edu", "only https"),
        ("ftp://example.edu", "only https"),
        ("file:///etc/passwd", "only https"),
        ("data:text/plain,hello", "only https"),
        ("javascript:alert(1)", "only https"),
        ("https://", "needs a hostname"),
        ("https://user@example.edu", "credentials"),
        ("https://user:pw@example.edu", "credentials"),
        ("https://127.0.0.1/", "IP-literal"),
        ("https://[::1]/", "IP-literal"),
        ("https://192.168.1.10/", "IP-literal"),
        ("https://example.edu:8443/", "default HTTPS port"),
        ("", "must not be blank"),
    ],
)
def test_an_unsafe_url_is_refused_with_a_reason(url: str, reason: str) -> None:
    with pytest.raises(WebWatchUnsafeUrl) as raised:
        validate_watch_url(url)

    assert reason in raised.value.reason


def test_a_fragment_is_dropped() -> None:
    assert validate_watch_url("https://example.edu/a#part") == "https://example.edu/a"


# -------------------------------------------------------------------------- addresses


@pytest.mark.parametrize(
    "address", ["93.184.216.34", "1.1.1.1", "2606:4700:4700::1111", "8.8.8.8"]
)
def test_a_public_address_is_contactable(address: str) -> None:
    assert is_public_ip(address) is True


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.1.2.3",
        "::1",
        "10.0.0.1",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.1.1",
        "169.254.1.1",
        "fc00::1",
        "fe80::1",
        "224.0.0.1",
        "0.0.0.0",
        "255.255.255.255",
        "240.0.0.1",
        "100.64.0.1",
    ],
)
def test_a_non_public_address_is_refused(address: str) -> None:
    assert is_public_ip(address) is False


@pytest.mark.parametrize("value", ["", "not-an-address", "example.edu", "999.999.999.999"])
def test_something_that_is_not_an_address_is_refused(value: str) -> None:
    assert is_public_ip(value) is False


# ------------------------------------------------------------------------- content type


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("text/html", WebContentType.HTML),
        ("text/html; charset=utf-8", WebContentType.HTML),
        ("TEXT/PLAIN", WebContentType.PLAIN),
        ("application/json;charset=UTF-8", WebContentType.JSON),
    ],
)
def test_a_supported_content_type_is_parsed(header: str, expected: WebContentType) -> None:
    assert WebContentType.parse(header) is expected


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "application/pdf",
        "image/png",
        "application/zip",
        "text/csv",
        "application/octet-stream",
    ],
)
def test_an_unsupported_content_type_has_no_meaning(header: str | None) -> None:
    assert WebContentType.parse(header) is None


# ------------------------------------------------------------------------ normalization


def test_crlf_becomes_lf_and_trailing_space_is_trimmed() -> None:
    assert normalize_text("a  \r\nb\t\r\n") == "a\nb"


def test_leading_and_trailing_blank_lines_are_dropped() -> None:
    assert normalize_text("\n\nhello\n\n\n") == "hello"


def test_excessive_blank_lines_collapse_deterministically() -> None:
    assert normalize_text("a\n\n\n\n\nb") == "a\n\n\nb"
    assert normalize_text("a\n\nb") == "a\n\nb"


def test_line_structure_survives_normalization() -> None:
    """The change context is a line diff, so flattening a page would destroy it."""
    assert normalize_text("first\nsecond\nthird") == "first\nsecond\nthird"


def test_normalization_is_idempotent() -> None:
    once = normalize_text("a \r\n\r\n\r\n\r\n b \n")

    assert normalize_text(once) == once


def test_normalization_is_bounded() -> None:
    huge = "\n".join("x" * 100 for _ in range(MAX_NORMALIZED_CHARS // 50))

    assert len(normalize_text(huge)) <= MAX_NORMALIZED_CHARS


def test_the_content_hash_is_sha256_of_the_normalized_utf8_text() -> None:
    text = normalize_text("héllo\nworld")

    assert content_sha256(text) == content_sha256(normalize_text("héllo\r\nworld  "))
    assert len(content_sha256(text)) == 64
    assert content_sha256(text) != content_sha256("h\u00e9llo\nworld!")


# ----------------------------------------------------------------------- observations


def _observation(**overrides: object) -> WebObservation:
    values: dict[str, object] = {
        "target_id": "course-notices",
        "url": "https://example.edu/notices",
        "content_sha256": "a" * 64,
        "storage_key": "web/snapshots/aa/aaa.txt",
        "fetched_at": NOW,
    }
    values.update(overrides)
    return WebObservation(**values)  # type: ignore[arg-type]


def test_a_baseline_observation_replaces_nothing() -> None:
    baseline = _observation(is_baseline=True)

    assert baseline.is_baseline is True
    assert baseline.previous_observation_id is None
    with pytest.raises(InvalidWebTarget):
        _observation(is_baseline=True, previous_observation_id=new_web_observation_id())


def test_a_change_observation_names_its_predecessor() -> None:
    previous = new_web_observation_id()

    assert _observation(previous_observation_id=previous).previous_observation_id == previous
    with pytest.raises(InvalidWebTarget):
        _observation()


def test_an_observation_needs_a_hash_a_key_and_an_aware_timestamp() -> None:
    with pytest.raises(InvalidWebTarget):
        _observation(content_sha256="short")
    with pytest.raises(InvalidWebTarget):
        _observation(storage_key="   ")
    with pytest.raises(InvalidWebTarget):
        _observation(fetched_at=datetime(2026, 9, 25, 9, 0))


# ----------------------------------------------------------------------------- state


def _state(**overrides: object) -> WebWatchState:
    values: dict[str, object] = {
        "target_id": "course-notices",
        "url": "https://example.edu/notices",
        "updated_at": NOW,
    }
    values.update(overrides)
    return WebWatchState(**values)  # type: ignore[arg-type]


def test_a_state_without_a_hash_has_no_baseline() -> None:
    assert _state().has_baseline is False
    assert _state(content_sha256="a" * 64).has_baseline is True


def test_a_fresh_target_fetches_unconditionally() -> None:
    assert _state().conditional_headers(full_fetch_every=24, reset=False) is None


def test_a_baseline_target_sends_its_validators() -> None:
    state = _state(
        content_sha256="a" * 64,
        etag='W/"1"',
        last_modified="Wed, 24 Sep 2026 09:00:00 GMT",
    )

    assert state.conditional_headers(full_fetch_every=24, reset=False) == (
        'W/"1"',
        "Wed, 24 Sep 2026 09:00:00 GMT",
    )


def test_a_target_with_no_validators_fetches_unconditionally() -> None:
    assert (
        _state(content_sha256="a" * 64).conditional_headers(
            full_fetch_every=24, reset=False
        )
        is None
    )


def test_a_full_fetch_is_forced_periodically() -> None:
    """§17: the validators are an optimization; the periodic full fetch is the correctness rule."""
    state = _state(content_sha256="a" * 64, etag="v1", checks_since_full=23)

    assert state.conditional_headers(full_fetch_every=24, reset=False) is not None
    assert (
        _state(content_sha256="a" * 64, etag="v1", checks_since_full=24)
        .conditional_headers(full_fetch_every=24, reset=False)
        is None
    )


def test_a_reset_target_fetches_unconditionally() -> None:
    """A changed URL must not inherit the old URL's validators or its baseline."""
    state = _state(content_sha256="a" * 64, etag="v1")

    assert state.conditional_headers(full_fetch_every=24, reset=True) is None


def test_state_timestamps_must_be_aware_and_counters_non_negative() -> None:
    with pytest.raises(InvalidWebTarget):
        _state(checks_since_full=-1)
    with pytest.raises(InvalidWebTarget):
        _state(last_checked_at=datetime(2026, 9, 25, 9, 0))
    assert _state(last_changed_at=NOW + timedelta(minutes=1))
