"""Unit tests for deterministic, source-traceable chunking (ADR-0012)."""

from __future__ import annotations

from assistant.domain.chunking import TARGET_CHUNK_CHARS, chunk_lines, chunk_pages


def _non_empty_lines(text: str) -> list[str]:
    return [line for line in text.replace("\r\n", "\n").split("\n") if line.strip()]


def test_chunks_preserve_line_order_and_numbers() -> None:
    text = "\n".join(f"line {number}" for number in range(1, 21))

    chunks = chunk_lines(text, target_chars=40)

    assert len(chunks) > 1
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))
    assert chunks[0].source_span.line_start == 1
    seen: list[str] = []
    for chunk in chunks:
        start = chunk.source_span.line_start
        end = chunk.source_span.line_end
        assert start is not None and end is not None and start <= end
        seen.extend(chunk.content.split("\n"))
    assert seen == _non_empty_lines(text)


def test_blank_lines_never_form_their_own_chunk() -> None:
    chunks = chunk_lines("\n\n\n")

    assert chunks == []


def test_blank_lines_inside_a_chunk_are_kept_in_place() -> None:
    chunks = chunk_lines("first\n\nthird\n")

    assert len(chunks) == 1
    assert chunks[0].content == "first\n\nthird\n"
    assert chunks[0].source_span.line_start == 1
    assert chunks[0].source_span.line_end == 4


def test_a_very_long_line_becomes_one_honest_chunk() -> None:
    text = "x" * (TARGET_CHUNK_CHARS * 3)

    chunks = chunk_lines(text, target_chars=100, max_chars=200)

    assert len(chunks) == 1
    assert len(chunks[0].content) == len(text)
    assert chunks[0].source_span.line_start == 1
    assert chunks[0].source_span.line_end == 1


def test_crlf_is_normalised_without_renumbering() -> None:
    text = "first\r\nsecond\r\nthird\r\n"

    chunks = chunk_lines(text)

    assert "\r" not in chunks[0].content
    assert chunks[0].content == "first\nsecond\nthird\n"
    assert chunks[0].source_span.line_end == 4


def test_start_ordinal_is_honoured() -> None:
    chunks = chunk_lines("a\nb\n", start_ordinal=5)

    assert [chunk.ordinal for chunk in chunks] == [5]


def test_chunk_sizes_are_validated() -> None:
    try:
        chunk_lines("text", target_chars=0)
    except ValueError as exc:
        assert "chunk sizes" in str(exc)
    else:  # pragma: no cover - the call above must raise
        raise AssertionError("target_chars=0 must be rejected")


def test_pdf_chunks_keep_their_page_and_share_one_ordinal_sequence() -> None:
    pages = [(1, "page one text"), (2, "page two text"), (3, "  ")]

    chunks = chunk_pages(pages)

    assert [chunk.source_span.page_number for chunk in chunks] == [1, 2]
    assert [chunk.ordinal for chunk in chunks] == [0, 1]
    assert all(chunk.source_span.line_start is None for chunk in chunks)


def test_a_long_pdf_page_splits_but_keeps_one_page_number() -> None:
    paragraphs = "\n\n".join("paragraph " * 20 for _ in range(5))

    chunks = chunk_pages([(7, paragraphs)], target_chars=200, max_chars=300)

    assert len(chunks) > 1
    assert {chunk.source_span.page_number for chunk in chunks} == {7}
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))

