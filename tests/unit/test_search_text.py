"""Unit tests for the escaping rules that keep user text out of query syntax (ADR-0012)."""

from __future__ import annotations

import pytest

from assistant.store.search_text import excerpt, fts5_phrase, like_pattern


def test_phrase_query_quotes_the_whole_input() -> None:
    assert fts5_phrase("deadline") == '"deadline"'


def test_phrase_query_escapes_embedded_quotes() -> None:
    assert fts5_phrase('say "hi"') == '"say ""hi"""'


@pytest.mark.parametrize(
    "query",
    ["a:b OR NEAR(x)", "*", "content:secret", "(a OR b)", "a*  b"],
)
def test_ft_syntax_characters_are_wrapped_not_executed(query: str) -> None:
    phrase = fts5_phrase(query)

    assert phrase.startswith('"')
    assert phrase.endswith('"')
    assert query.strip('"') in phrase


def test_like_pattern_escapes_wildcards() -> None:
    assert like_pattern("50%") == r"%50\%%"
    assert like_pattern("a_b") == r"%a\_b%"
    assert like_pattern(r"back\slash") == r"%back\\slash%"


def test_excerpt_windows_around_the_match() -> None:
    content = "start " + "x" * 500 + " needle " + "y" * 500 + " end"

    snippet = excerpt(content, "needle", limit=100)

    assert "needle" in snippet
    assert len(snippet) <= 103


def test_excerpt_falls_back_to_the_head_and_collapses_whitespace() -> None:
    snippet = excerpt("line one\n\nline   two", "absent", limit=100)

    assert snippet == "line one line two"
    assert "\n" not in snippet


def test_excerpt_validates_its_limit() -> None:
    with pytest.raises(ValueError, match="limit"):
        excerpt("content", "x", limit=0)

