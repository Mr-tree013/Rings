"""Deterministic text extraction and decoding for watcher responses (ADR-0029).

Two rules make a watcher trustworthy, and both live here:

- **what is extracted is text the page actually shows.** `script`, `style` and `noscript` content is
  dropped, because a page's JavaScript is not its message and a hash that moved because a build id
  changed would be noise. Nothing is fetched — no images, no framesheets, no frames;
- **decoding never fails.** A response that is not valid UTF-8 is decoded with deterministic
  replacement characters rather than raising, so a page with one stray byte still produces a stable
  hash instead of an error that hides a real change.

The output keeps line structure: the change context sent to a model is a line diff, and flattening a
page into one blob would destroy exactly the information the analysis needs.
"""

from __future__ import annotations

from html.parser import HTMLParser

from assistant.domain.web_watch import WebContentType, normalize_text

_IGNORED_ELEMENTS = frozenset({"script", "style", "noscript"})
_BLOCK_ELEMENTS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)


class _TextCollector(HTMLParser):
    """Collects visible text, one line per block element."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        lowered = tag.lower()
        if lowered in _IGNORED_ELEMENTS:
            self._ignored_depth += 1
        elif lowered in _BLOCK_ELEMENTS and self._ignored_depth == 0:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in _IGNORED_ELEMENTS:
            self._ignored_depth = max(0, self._ignored_depth - 1)
        elif lowered in _BLOCK_ELEMENTS and self._ignored_depth == 0:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        self._chunks.append(data)

    def text(self) -> str:
        """The collected text, before normalization."""
        return "".join(self._chunks)


def decode_body(raw: bytes) -> str:
    """Decode response bytes as UTF-8, replacing anything that is not valid UTF-8.

    Deterministic on purpose: the same bytes always produce the same text, so the same page always
    produces the same hash.
    """
    return raw.decode("utf-8", errors="replace")


def extract_text(content_type: WebContentType, raw: bytes) -> str:
    """Turn a response body into the text a watcher compares.

    HTML is parsed with the standard library parser and its script/style content dropped; plain
    text and JSON are used as they arrive. Everything is then normalized line by line.
    """
    decoded = decode_body(raw)
    if content_type is WebContentType.HTML:
        parser = _TextCollector()
        parser.feed(decoded)
        parser.close()
        return normalize_text(parser.text())
    return normalize_text(decoded)


__all__ = ["decode_body", "extract_text"]
