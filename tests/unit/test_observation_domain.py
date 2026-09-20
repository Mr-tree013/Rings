"""Manual input and observation analyses as values (ADR-0029).

Two things are being pinned here. Manual text is bounded, fingerprinted and source-labelled — and
it can never carry a channel with executable semantics. An analysis can classify, summarise and
name candidates, and there is nowhere in it to put a command, a task, a URL or a tool call.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from assistant.domain.errors import InvalidManualInput, InvalidObservationAnalysis
from assistant.domain.manual_input import (
    MANUAL_INPUT_MAX_CHARS,
    ManualInput,
    ManualInputSource,
    manual_input_sha256,
    new_manual_input_id,
)
from assistant.domain.observation_analysis import (
    MAX_ACTION_CANDIDATES,
    MAX_CANDIDATE_TEXT_CHARS,
    MAX_SUMMARY_CHARS,
    ObservationActionCandidate,
    ObservationAnalysis,
    ObservationCategory,
    ObservationTemporalKind,
    observation_analysis_input_fingerprint,
)

NOW = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)


# ------------------------------------------------------------------------ manual input


def test_manual_input_is_trimmed_fingerprinted_and_labelled() -> None:
    manual_input = ManualInput(
        text="  Forwarded: the lab report is due Friday.  ",
        source=ManualInputSource.QQ_FORWARD,
        created_at=NOW,
    )

    assert manual_input.text == "Forwarded: the lab report is due Friday."
    assert manual_input.source is ManualInputSource.QQ_FORWARD
    assert manual_input.content_sha256 == manual_input_sha256(manual_input.text)


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_blank_manual_input_is_refused(text: str) -> None:
    with pytest.raises(InvalidManualInput):
        ManualInput(text=text, source=ManualInputSource.MANUAL, created_at=NOW)


def test_manual_input_is_bounded() -> None:
    assert ManualInput(
        text="x" * MANUAL_INPUT_MAX_CHARS,
        source=ManualInputSource.MANUAL,
        created_at=NOW,
    )
    with pytest.raises(InvalidManualInput):
        ManualInput(
            text="x" * (MANUAL_INPUT_MAX_CHARS + 1),
            source=ManualInputSource.MANUAL,
            created_at=NOW,
        )


def test_manual_input_sources_are_a_closed_set_of_names() -> None:
    assert {item.value for item in ManualInputSource} == {"manual", "qq-forward", "other"}
    with pytest.raises(InvalidManualInput):
        ManualInput(text="x", source="shell", created_at=NOW)  # type: ignore[arg-type]


def test_manual_input_needs_an_aware_timestamp_and_a_consistent_hash() -> None:
    with pytest.raises(InvalidManualInput):
        ManualInput(
            text="x", source=ManualInputSource.MANUAL, created_at=datetime(2026, 9, 25)
        )
    with pytest.raises(InvalidManualInput):
        ManualInput(
            text="x",
            source=ManualInputSource.MANUAL,
            created_at=NOW,
            content_sha256="f" * 64,
        )


def test_manual_input_identities_are_unique() -> None:
    assert new_manual_input_id() != new_manual_input_id()


def test_a_manual_input_preview_is_single_line() -> None:
    manual_input = ManualInput(
        text="first\nsecond", source=ManualInputSource.MANUAL, created_at=NOW
    )

    assert manual_input.preview() == "first second"


# ------------------------------------------------------------------- analysis values


def _candidate(**overrides: object) -> ObservationActionCandidate:
    values: dict[str, object] = {"text": "Submit the lab report"}
    values.update(overrides)
    return ObservationActionCandidate(**values)  # type: ignore[arg-type]


def _analysis(**overrides: object) -> ObservationAnalysis:
    values: dict[str, object] = {
        "event_id": uuid4(),
        "source_kind": "manual.input.received",
        "analyzer_version": 1,
        "input_fingerprint": "a" * 64,
        "category": ObservationCategory.ACTIONABLE,
        "summary": "The notice asks for a report.",
        "created_at": NOW,
    }
    values.update(overrides)
    return ObservationAnalysis(**values)  # type: ignore[arg-type]


def test_a_candidate_carries_its_temporal_kind_separately_from_its_text() -> None:
    candidate = _candidate(
        temporal_kind=ObservationTemporalKind.DEADLINE, time_text="Friday 23:59"
    )

    assert candidate.temporal_kind is ObservationTemporalKind.DEADLINE
    assert candidate.time_text == "Friday 23:59"
    assert ObservationTemporalKind.EVENT_START is not ObservationTemporalKind.DEADLINE
    assert ObservationTemporalKind.EVENT_START.value == "event-start"


def test_a_candidate_text_is_bounded_and_non_blank() -> None:
    assert _candidate(text="x" * MAX_CANDIDATE_TEXT_CHARS)
    with pytest.raises(InvalidObservationAnalysis):
        _candidate(text="   ")
    with pytest.raises(InvalidObservationAnalysis):
        _candidate(text="x" * (MAX_CANDIDATE_TEXT_CHARS + 1))


def test_a_temporal_kind_and_its_evidence_agree() -> None:
    # A timed kind carries the text or the instant it was read from…
    assert _candidate(
        temporal_kind=ObservationTemporalKind.DEADLINE, time_text="Friday"
    )
    # …and is refused when it carries neither, because "there is a deadline" without evidence is
    # not something a person can act on.
    with pytest.raises(InvalidObservationAnalysis):
        _candidate(temporal_kind=ObservationTemporalKind.DEADLINE)
    with pytest.raises(InvalidObservationAnalysis):
        _candidate(interpreted_at=NOW)  # kind is none


def test_a_candidate_round_trips_through_its_stored_payload() -> None:
    candidate = _candidate(
        temporal_kind=ObservationTemporalKind.EVENT_START,
        time_text="Oct 25",
        interpreted_at=NOW,
    )

    assert ObservationActionCandidate.from_payload(candidate.to_payload()) == candidate
    with pytest.raises(InvalidObservationAnalysis):
        ObservationActionCandidate.from_payload({"text": "x"})
    with pytest.raises(InvalidObservationAnalysis):
        ObservationActionCandidate.from_payload(
            {
                "text": "x",
                "temporal_kind": "whenever",
                "time_text": None,
                "interpreted_at": None,
            }
        )


def test_an_analysis_summary_is_bounded() -> None:
    assert _analysis(summary="x" * MAX_SUMMARY_CHARS)
    with pytest.raises(InvalidObservationAnalysis):
        _analysis(summary="   ")
    with pytest.raises(InvalidObservationAnalysis):
        _analysis(summary="x" * (MAX_SUMMARY_CHARS + 1))


def test_an_analysis_has_a_bounded_number_of_candidates() -> None:
    assert _analysis(
        action_candidates=tuple(_candidate() for _ in range(MAX_ACTION_CANDIDATES))
    )
    with pytest.raises(InvalidObservationAnalysis):
        _analysis(
            action_candidates=tuple(_candidate() for _ in range(MAX_ACTION_CANDIDATES + 1))
        )


def test_an_analysis_needs_a_known_category_and_a_real_fingerprint() -> None:
    with pytest.raises(InvalidObservationAnalysis):
        _analysis(category="urgent")
    with pytest.raises(InvalidObservationAnalysis):
        _analysis(input_fingerprint="short")
    with pytest.raises(InvalidObservationAnalysis):
        _analysis(analyzer_version=0)


def test_the_candidates_payload_is_canonical_json() -> None:
    analysis = _analysis(action_candidates=(_candidate(text="Do the thing"),))

    assert analysis.candidates_payload().startswith('[{"interpreted_at":null,')
    assert analysis.candidates_payload() == analysis.candidates_payload()


def test_an_analysis_has_nowhere_to_put_a_command_or_an_action() -> None:
    """The shape is the boundary: there is no field for a task, a URL or a tool call."""
    fields = set(ObservationAnalysis.__dataclass_fields__)
    candidate_fields = set(ObservationActionCandidate.__dataclass_fields__)

    for forbidden in ("command", "task", "url", "tool", "reasoning", "confidence"):
        assert forbidden not in fields
        assert forbidden not in candidate_fields


# ------------------------------------------------------------------- fingerprints


def test_the_input_fingerprint_covers_the_versions_identity_and_context() -> None:
    arguments: dict[str, object] = {
        "analyzer_version": 1,
        "schema_version": 1,
        "event_type": "manual.input.received",
        "event_external_id": "manual-input:" + str(uuid4()),
        "source_identities": ("manual-input:x", "b" * 64),
        "context_fingerprint": "c" * 64,
    }
    baseline = observation_analysis_input_fingerprint(**arguments)  # type: ignore[arg-type]

    assert len(baseline) == 64
    for key, value in (
        ("analyzer_version", 2),
        ("schema_version", 2),
        ("event_type", "web.page.changed"),
        ("context_fingerprint", "d" * 64),
        ("source_identities", ("manual-input:y", "b" * 64)),
    ):
        variant = {**arguments, key: value}
        assert (
            observation_analysis_input_fingerprint(**variant) != baseline  # type: ignore[arg-type]
        )


def test_the_input_fingerprint_ignores_the_order_of_source_identities() -> None:
    first = observation_analysis_input_fingerprint(
        analyzer_version=1,
        schema_version=1,
        event_type="web.page.changed",
        event_external_id="observation:x",
        source_identities=("a", "b"),
        context_fingerprint="c" * 64,
    )
    second = observation_analysis_input_fingerprint(
        analyzer_version=1,
        schema_version=1,
        event_type="web.page.changed",
        event_external_id="observation:x",
        source_identities=("b", "a"),
        context_fingerprint="c" * 64,
    )

    assert first == second
