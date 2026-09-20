"""Deterministic keyword derivation for question-shaped searches (ADR-0019)."""

from __future__ import annotations

import pytest

from assistant.application.knowledge_search import (
    KEYWORD_MIN_CHARS,
    MAX_KEYWORD_TOKENS,
    derive_keyword_tokens,
)


def test_tokens_keep_the_question_order_without_stopwords_or_short_words() -> None:
    tokens = derive_keyword_tokens("What is the SE lab submission deadline for my report?")

    # "What", "is", "the", "for", "my" are function words; "SE" is below the trigram minimum.
    assert tokens == ("lab", "submission", "deadline", "report")


def test_short_words_are_dropped_because_the_index_cannot_match_them() -> None:
    tokens = derive_keyword_tokens("is it on os or the db key?")

    assert tokens == ("key",)  # two-character tokens are below the trigram minimum
    assert KEYWORD_MIN_CHARS == 3


def test_tokens_are_lowercased_and_deduplicated_in_first_appearance_order() -> None:
    assert derive_keyword_tokens("Deadline deadline DEADLINE report") == (
        "deadline",
        "report",
    )


def test_punctuation_is_a_separator_not_part_of_a_token() -> None:
    tokens = derive_keyword_tokens("deadline: October 23, 2026 — calculus!")

    assert tokens == ("deadline", "october", "2026", "calculus")


def test_unicode_letters_are_kept() -> None:
    assert derive_keyword_tokens("期末考试 сроки") == ("期末考试", "сроки")


def test_the_token_count_is_bounded() -> None:
    tokens = derive_keyword_tokens(" ".join(f"token{index}" for index in range(30)))

    assert len(tokens) == MAX_KEYWORD_TOKENS
    assert tokens[0] == "token0"


def test_a_query_without_usable_tokens_yields_nothing() -> None:
    assert derive_keyword_tokens("is it a to of?") == ()
    assert derive_keyword_tokens("") == ()


@pytest.mark.parametrize(
    "question",
    (
        "What is the deadline?",
        "what is the deadline?",
        "  WHAT IS THE DEADLINE?  ",
    ),
)
def test_derivation_is_deterministic(question: str) -> None:
    assert derive_keyword_tokens(question) == derive_keyword_tokens(question)
