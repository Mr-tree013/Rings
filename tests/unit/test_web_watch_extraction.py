"""Deterministic extraction and content-addressed snapshots (ADR-0029).

Extraction has one job: turn a response into the text a watcher compares, the same way every time.
Scripts and styles are dropped because a build id is not a change, and decoding never fails because
one stray byte should not hide a real one. Snapshots are addressed by their content, so the same
page is one object no matter how often it is seen.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from assistant.adapters.web_watch.extractor import decode_body, extract_text
from assistant.adapters.web_watch.snapshot_store import WebSnapshotStore
from assistant.domain.errors import WebSnapshotStorageError
from assistant.domain.web_watch import WebContentType

# ------------------------------------------------------------------------- extraction


def test_html_text_is_extracted_with_its_line_structure() -> None:
    html = b"<html><body><h1>Notices</h1><p>Registration closes Oct 20.</p></body></html>"

    text = extract_text(WebContentType.HTML, html)

    assert "Notices" in text
    assert "Registration closes Oct 20." in text
    assert "Notices" in text.splitlines()[0]


def test_script_style_and_noscript_content_is_dropped() -> None:
    """A page's JavaScript is not its message, and a build id must not look like a change."""
    html = (
        b"<html><head><style>body{color:red}</style>"
        b"<script>var build='2026-09-25-abc';</script></head>"
        b"<body>Real content<noscript>enable js</noscript></body></html>"
    )

    text = extract_text(WebContentType.HTML, html)

    assert "Real content" in text
    assert "build" not in text
    assert "color:red" not in text
    assert "enable js" not in text


def test_nothing_is_fetched_while_extracting() -> None:
    """An extractor that followed a link would be a crawler; the suite's socket guard proves it."""
    html = b'<html><body><img src="https://evil.example/x.png"><iframe src="/frame"></iframe>'
    html += b'</body></html>'

    assert extract_text(WebContentType.HTML, html).strip() == ""


def test_html_entities_are_decoded() -> None:
    assert extract_text(WebContentType.HTML, b"<p>a &amp; b &lt;c&gt;</p>") == "a & b <c>"


def test_plain_text_and_json_are_used_as_they_arrive() -> None:
    assert extract_text(WebContentType.PLAIN, b"plain  \r\ntext\n") == "plain\ntext"
    assert extract_text(WebContentType.JSON, b'{"a": 1}') == '{"a": 1}'


def test_decoding_never_fails_on_invalid_utf8() -> None:
    decoded = decode_body(b"ok \xff\xfe done")

    assert decoded.startswith("ok ")
    assert decoded.endswith(" done")
    assert decode_body(b"ok \xff\xfe done") == decoded  # deterministic


def test_a_script_only_change_does_not_move_the_hash() -> None:
    first = extract_text(WebContentType.HTML, b"<body>same<script>v1</script></body>")
    second = extract_text(WebContentType.HTML, b"<body>same<script>v2</script></body>")

    assert first == second


# -------------------------------------------------------------------------- snapshots


async def test_a_snapshot_is_stored_by_its_content_address(tmp_path: Path) -> None:
    store = WebSnapshotStore(tmp_path)

    digest, key = await store.store_text("hello\nworld")

    assert key == f"web/snapshots/{digest[:2]}/{digest}.txt"
    assert (tmp_path / key).read_text(encoding="utf-8") == "hello\nworld"
    assert await store.read_text(key) == "hello\nworld"
    assert digest == hashlib.sha256(b"hello\nworld").hexdigest()


async def test_storing_the_same_text_twice_reuses_one_object(tmp_path: Path) -> None:
    store = WebSnapshotStore(tmp_path)

    first_digest, first_key = await store.store_text("same")
    second_digest, second_key = await store.store_text("same")

    assert (first_digest, first_key) == (second_digest, second_key)
    assert len(list((tmp_path / "web" / "snapshots").rglob("*.txt"))) == 1
    assert not list((tmp_path / "web" / "snapshots").rglob("*.tmp"))


async def test_reading_a_missing_snapshot_fails(tmp_path: Path) -> None:
    store = WebSnapshotStore(tmp_path)

    with pytest.raises(WebSnapshotStorageError):
        await store.read_text("web/snapshots/aa/" + "a" * 64 + ".txt")


async def test_a_snapshot_that_disagrees_with_its_address_is_refused(tmp_path: Path) -> None:
    store = WebSnapshotStore(tmp_path)
    _digest, key = await store.store_text("original")
    (tmp_path / key).write_text("tampered", encoding="utf-8")

    with pytest.raises(WebSnapshotStorageError):
        await store.read_text(key)
