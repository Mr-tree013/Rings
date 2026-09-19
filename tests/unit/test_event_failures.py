"""Unit tests for bounded failure summaries."""

from __future__ import annotations

from assistant.application.event_failures import MAX_ERROR_LENGTH, format_event_failure


class SilentError(Exception):
    """An exception whose message is empty."""


def test_formats_the_exception_type_and_message() -> None:
    assert format_event_failure(RuntimeError("smtp timeout")) == "RuntimeError: smtp timeout"


def test_collapses_whitespace_into_a_single_line() -> None:
    summary = format_event_failure(ValueError("line one\n\tline two   line three"))

    assert summary == "ValueError: line one line two line three"


def test_falls_back_to_the_exception_type() -> None:
    assert format_event_failure(SilentError()) == "SilentError"


def test_truncates_a_long_message_to_the_limit() -> None:
    summary = format_event_failure(RuntimeError("x" * (MAX_ERROR_LENGTH * 2)))

    assert len(summary) == MAX_ERROR_LENGTH
    assert summary.startswith("RuntimeError: xxx")
    assert summary.endswith("...")


def test_keeps_a_message_that_exactly_fits() -> None:
    prefix = "RuntimeError: "
    message = "y" * (MAX_ERROR_LENGTH - len(prefix))

    summary = format_event_failure(RuntimeError(message))

    assert len(summary) == MAX_ERROR_LENGTH
    assert summary.endswith("y")

