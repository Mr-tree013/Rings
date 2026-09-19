"""Turning plain user text into safe SQLite search syntax (ADR-0012).

User input is *text*, never query syntax: `a:b OR "x"`, `NEAR(...)`, `*`, parentheses, `%`
and `_` are all just characters to look for. Everything in this module is pure so the
escaping rules can be tested directly.
"""

from __future__ import annotations

from assistant.domain.knowledge import DEFAULT_SNIPPET_CHARS

FTS_MIN_QUERY_CHARS = 3
"""FTS5 trigram needs at least three characters; shorter queries use a literal LIKE search."""

LIKE_ESCAPE = "\\"


def fts5_phrase(query: str) -> str:
    """Wrap user text in a quoted FTS5 phrase, escaping embedded double quotes.

    Quoting keeps every FTS5 operator inside the phrase, so a query can only ever search for
    its own characters.
    """
    return '"' + query.replace('"', '""') + '"'


def like_pattern(query: str) -> str:
    """Build a `LIKE` pattern that matches `query` literally.

    `%`, `_` and the escape character itself are escaped, so they can never act as
    wildcards. Pair with `LIKE ? ESCAPE '\\'`.
    """
    escaped = (
        query.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", f"{LIKE_ESCAPE}%")
        .replace("_", f"{LIKE_ESCAPE}_")
    )
    return f"%{escaped}%"


def excerpt(
    content: str, needle: str, *, limit: int = DEFAULT_SNIPPET_CHARS
) -> str:
    """Return a bounded, single-line excerpt of `content` around `needle`.

    Deterministic and whitespace-collapsed: search results carry a short snippet, never a
    whole chunk.
    """
    if limit < 1:
        raise ValueError("limit must be a positive integer")
    collapsed = " ".join(content.split())
    position = collapsed.lower().find(needle.lower()) if needle else -1
    if position < 0:
        start = 0
        window = collapsed[:limit]
    else:
        start = max(0, position - limit // 3)
        window = collapsed[start : start + limit]
    if len(collapsed) > len(window):
        return window.rstrip() + "..."
    return window


__all__ = ["FTS_MIN_QUERY_CHARS", "LIKE_ESCAPE", "excerpt", "fts5_phrase", "like_pattern"]

