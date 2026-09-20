"""The interpreter's JSON Schema, checked against realistic model answers (ADR-0018)."""

from __future__ import annotations

import pytest
from jsonschema.validators import Draft202012Validator

from assistant.application.interpreter_schema import (
    INTERPRETER_SCHEMA_NAME,
    INTERPRETER_SCHEMA_V1,
)
from assistant.application.structured_model import validate_json_schema

TASK_ID = "11111111-1111-4111-8111-111111111111"


def _valid(payload: dict[str, object]) -> bool:
    return not list(Draft202012Validator(INTERPRETER_SCHEMA_V1.schema).iter_errors(payload))


def test_the_schema_is_a_valid_draft_2020_12_schema() -> None:
    validate_json_schema(INTERPRETER_SCHEMA_V1)

    assert INTERPRETER_SCHEMA_V1.name == INTERPRETER_SCHEMA_NAME
    assert INTERPRETER_SCHEMA_V1.schema["additionalProperties"] is False


def test_ready_examples_validate_for_every_supported_command() -> None:
    commands = [
        {
            "kind": "create_task",
            "title": "Write SE lab",
            "description": None,
            "priority": "high",
            "estimated_minutes": 300,
            "deadline": None,
        },
        {"kind": "complete_task", "task_id": TASK_ID},
        {"kind": "cancel_task", "task_id": TASK_ID},
        {
            "kind": "set_deadline",
            "task_id": TASK_ID,
            "due_at": "2026-10-23T23:59:00+08:00",
        },
        {"kind": "clear_deadline", "task_id": TASK_ID},
        {
            "kind": "create_calendar_event",
            "title": "Class",
            "description": None,
            "starts_at": "2026-10-21T10:00:00+08:00",
            "ends_at": "2026-10-21T12:00:00+08:00",
        },
        {"kind": "request_week_plan", "next_week": True},
    ]

    for command in commands:
        assert _valid(
            {"status": "ready", "command": command, "question": None, "reason": None}
        ), command


def test_clarification_and_unsupported_examples_validate() -> None:
    assert _valid(
        {
            "status": "needs_clarification",
            "command": None,
            "question": "Which task do you mean?",
            "reason": None,
        }
    )
    assert _valid(
        {
            "status": "unsupported",
            "command": None,
            "question": None,
            "reason": "Sending mail is not supported yet.",
        }
    )


@pytest.mark.parametrize(
    "payload",
    (
        # An unknown capability must fail here, not degrade into "unsupported".
        {
            "status": "ready",
            "command": {"kind": "send_email", "to": "professor"},
            "question": None,
            "reason": None,
        },
        # No free-form argument bag.
        {
            "status": "ready",
            "command": {"kind": "complete_task", "task_id": TASK_ID, "arguments": {}},
            "question": None,
            "reason": None,
        },
        # Every field is required, so "absent" and "null" can never be confused.
        {
            "status": "ready",
            "command": {"kind": "complete_task", "task_id": TASK_ID},
            "question": None,
        },
        # A ready answer cannot also ask a question.
        {
            "status": "ready",
            "command": {"kind": "clear_deadline", "task_id": TASK_ID},
            "question": "are you sure?",
            "reason": None,
        },
        # A clarification cannot carry a command.
        {
            "status": "needs_clarification",
            "command": {"kind": "complete_task", "task_id": TASK_ID},
            "question": "which one?",
            "reason": None,
        },
        # Empty strings are not answers.
        {"status": "needs_clarification", "command": None, "question": "", "reason": None},
        {"status": "unsupported", "command": None, "question": None, "reason": ""},
        # Long explanations are not wanted either.
        {"status": "unsupported", "command": None, "question": None, "reason": "x" * 501},
        # No reasoning, no confidence, no extra keys anywhere.
        {
            "status": "unsupported",
            "command": None,
            "question": None,
            "reason": "no",
            "confidence": 0.9,
        },
        {"status": "thinking", "command": None, "question": None, "reason": "hmm"},
    ),
)
def test_answers_that_must_not_validate(payload: dict[str, object]) -> None:
    assert not _valid(payload)


def test_create_task_rejects_an_empty_title_and_a_zero_estimate() -> None:
    base: dict[str, object] = {"status": "ready", "question": None, "reason": None}
    empty_title = {
        "kind": "create_task",
        "title": "",
        "description": None,
        "priority": "normal",
        "estimated_minutes": None,
        "deadline": None,
    }
    zero_estimate = {**empty_title, "title": "x", "estimated_minutes": 0}

    assert not _valid({**base, "command": empty_title})
    assert not _valid({**base, "command": zero_estimate})


def test_the_string_null_is_not_the_same_as_json_null() -> None:
    """`"null"` as a *string* is a model mistake, and must not slip through as "unspecified"."""
    command = {
        "kind": "create_task",
        "title": "x",
        "description": None,
        "priority": "null",
        "estimated_minutes": None,
        "deadline": None,
    }

    assert not _valid(
        {"status": "ready", "command": command, "question": None, "reason": None}
    )
