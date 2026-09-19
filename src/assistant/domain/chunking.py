"""Deterministic, source-traceable chunking of extracted text (ADR-0012).

Pure functions only: chunking is an algorithm over text and domain values, so it lives next
to the domain rather than inside an adapter or the application layer. Chunks are line-aware
for text files and page-aware for PDFs, never token-based, and never renumber lines they did
not create.
"""

from __future__ import annotations

from collections.abc import Sequence

from assistant.domain.knowledge import ExtractedChunk, SourceSpan

TARGET_CHUNK_CHARS = 2000
MAX_CHUNK_CHARS = 4000


def chunk_lines(
    text: str,
    *,
    start_ordinal: int = 0,
    target_chars: int = TARGET_CHUNK_CHARS,
    max_chars: int = MAX_CHUNK_CHARS,
) -> list[ExtractedChunk]:
    """Split text into line-ranged chunks, preserving order and line numbers.

    Lines are 1-based and inclusive. A single line longer than `max_chars` becomes its own
    (longer) chunk rather than a fabricated range, and blank lines are never chunked alone.
    """
    if target_chars < 1 or max_chars < target_chars:
        raise ValueError("chunk sizes must satisfy 1 <= target_chars <= max_chars")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    chunks: list[ExtractedChunk] = []
    pending: list[str] = []
    start_line = 0

    def flush(end_line: int) -> None:
        if not pending:
            return
        content = "\n".join(pending)
        if content.strip():
            chunks.append(
                ExtractedChunk(
                    ordinal=start_ordinal + len(chunks),
                    content=content,
                    source_span=SourceSpan.lines(start_line, end_line),
                )
            )
        pending.clear()

    for number, line in enumerate(lines, start=1):
        if not pending:
            start_line = number
        pending.append(line)
        if sum(len(item) + 1 for item in pending) >= target_chars:
            flush(number)
    flush(len(lines))
    return chunks


def chunk_pages(
    pages: Sequence[tuple[int, str]],
    *,
    start_ordinal: int = 0,
    target_chars: int = TARGET_CHUNK_CHARS,
    max_chars: int = MAX_CHUNK_CHARS,
) -> list[ExtractedChunk]:
    """Chunk per-page text, keeping every chunk's span on the page it came from."""
    chunks: list[ExtractedChunk] = []
    for page_number, text in pages:
        if not text.strip():
            continue
        for content in _split_long_text(text, target_chars=target_chars, max_chars=max_chars):
            chunks.append(
                ExtractedChunk(
                    ordinal=start_ordinal + len(chunks),
                    content=content,
                    source_span=SourceSpan.page(page_number),
                )
            )
    return chunks


def _split_long_text(text: str, *, target_chars: int, max_chars: int) -> list[str]:
    """Split one page of text into paragraph-sized pieces bounded by `max_chars`."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    pieces: list[str] = []
    current: list[str] = []
    current_length = 0
    for paragraph in normalized.split("\n\n"):
        stripped = paragraph.strip("\n")
        if not stripped.strip():
            continue
        if current and current_length + len(stripped) > max_chars:
            pieces.append("\n\n".join(current))
            current = []
            current_length = 0
        current.append(stripped)
        current_length += len(stripped) + 2
        if current_length >= target_chars:
            pieces.append("\n\n".join(current))
            current = []
            current_length = 0
    if current:
        pieces.append("\n\n".join(current))
    return [piece for piece in pieces if piece.strip()]


__all__ = ["MAX_CHUNK_CHARS", "TARGET_CHUNK_CHARS", "chunk_lines", "chunk_pages"]
