"""Turning schema-valid output into a durable analysis, or refusing it (ADR-0029).

The schema fixes the shape; these rules are what a JSON Schema cannot say — a deadline needs the
text it was read from, an instant needs an explicit offset, and a wall-clock value is refused
rather than guessed at.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from assistant.application.observation_analysis import parse_observation_analysis_output
from assistant.domain.errors import InvalidObservationAnalysis
from assistant.domain.observation_analysis import (
    ObservationCategory,
    ObservationTemporalKind,
)

NOW = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)


def _payload(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "category": "actionable",
        "summary": "Registration closes on Oct 20.",
        "action_candidates": [
            {
                "text": "Register before the deadline",
                "temporal_kind": "deadline",
                "time_text": "Oct 20",
                "interpreted_at": "2026-10-20T23:59:00+08:00",
            }
        ],
    }
    values.update(overrides)
    return values


def _parse(payload: dict[str, object]):
    return parse_observation_analysis_output(
        payload,
        event_id=uuid4(),
        source_kind="web.page.changed",
        input_fingerprint="a" * 64,
        now=NOW,
    )


def test_a_well_formed_analysis_is_parsed() -> None:
    analysis = _parse(_payload())

    assert analysis.category is ObservationCategory.ACTIONABLE
    assert analysis.source_kind == "web.page.changed"
    assert analysis.analyzer_version == 1
    candidate = analysis.action_candidates[0]
    assert candidate.temporal_kind is ObservationTemporalKind.DEADLINE
    assert candidate.interpreted_at == datetime(2026, 10, 20, 15, 59, tzinfo=UTC)


def test_a_deadline_and_an_event_start_stay_different_kinds() -> None:
    payload = _payload(
        action_candidates=[
            {
                "text": "Submit the form",
                "temporal_kind": "deadline",
                "time_text": "Friday",
                "interpreted_at": None,
            },
            {
                "text": "Workshop begins",
                "temporal_kind": "event-start",
                "time_text": "Oct 25",
                "interpreted_at": None,
            },
        ]
    )

    analysis = _parse(payload)

    kinds = [item.temporal_kind for item in analysis.action_candidates]
    assert kinds == [ObservationTemporalKind.DEADLINE, ObservationTemporalKind.EVENT_START]


def test_a_candidate_with_no_time_is_allowed_to_have_no_kind() -> None:
    analysis = _parse(
        _payload(
            action_candidates=[
                {
                    "text": "Read the notice",
                    "temporal_kind": "none",
                    "time_text": None,
                    "interpreted_at": None,
                }
            ]
        )
    )

    assert analysis.action_candidates[0].temporal_kind is ObservationTemporalKind.NONE


def test_a_timed_candidate_without_evidence_is_refused() -> None:
    with pytest.raises(InvalidObservationAnalysis):
        _parse(
            _payload(
                action_candidates=[
                    {
                        "text": "There is a deadline somewhere",
                        "temporal_kind": "deadline",
                        "time_text": None,
                        "interpreted_at": None,
                    }
                ]
            )
        )


@pytest.mark.parametrize(
    "instant", ["2026-10-20T23:59:00", "2026-10-20 23:59", "tomorrow", "not-a-date"]
)
def test_an_instant_without_an_offset_is_refused(instant: str) -> None:
    with pytest.raises(InvalidObservationAnalysis):
        _parse(
            _payload(
                action_candidates=[
                    {
                        "text": "Register",
                        "temporal_kind": "deadline",
                        "time_text": "Oct 20",
                        "interpreted_at": instant,
                    }
                ]
            )
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"summary": "x", "action_candidates": []},
        {"category": "urgent", "summary": "x", "action_candidates": []},
        {"category": "actionable", "action_candidates": []},
        {"category": "actionable", "summary": "x", "action_candidates": "none"},
        {"category": "actionable", "summary": "x", "action_candidates": ["not-an-object"]},
    ],
)
def test_a_malformed_analysis_is_refused(payload: dict[str, object]) -> None:
    with pytest.raises(InvalidObservationAnalysis):
        _parse(payload)
