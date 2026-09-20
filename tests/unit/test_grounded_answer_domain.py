"""Grounding domain invariants: evidence, citations and answers (ADR-0019)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from assistant.domain.errors import (
    GroundedAnswerInvalidCitation,
    InvalidGroundedAnswer,
)
from assistant.domain.grounded_answer import (
    EvidenceId,
    GroundedAnswer,
    GroundedAnswerResult,
    GroundedAnswerSegment,
    GroundedAnswerStatus,
    KnowledgeEvidence,
    validate_citations,
)
from assistant.domain.knowledge import SourceSpan
from assistant.domain.storage import StorageUri

ROOT_ID = "archive-main"
URI = StorageUri.parse(f"vault://{ROOT_ID}/Courses/SE/notice.md")


def _evidence(source_id: str = "S1", *, content: str = "The deadline is October 23."):
    return KnowledgeEvidence(
        id=EvidenceId(source_id),
        root_id=ROOT_ID,
        entry_id=uuid4(),
        chunk_id=uuid4(),
        logical_uri=URI,
        source_span=SourceSpan.lines(18, 31),
        content=content,
    )


# ------------------------------------------------------------------------ evidence


@pytest.mark.parametrize("value", ["S1", "S2", "S10", "S1234"])
def test_evidence_ids_follow_the_documented_shape(value: str) -> None:
    assert str(EvidenceId(value)) == value


@pytest.mark.parametrize("value", ["", "S", "S0", "s1", "1", "S1a", "X1", "S-1", " S1"])
def test_other_evidence_ids_are_rejected(value: str) -> None:
    with pytest.raises(InvalidGroundedAnswer):
        EvidenceId(value)


def test_numbering_starts_at_one_and_is_derived_locally() -> None:
    assert EvidenceId.numbered(1).value == "S1"
    assert EvidenceId.numbered(12).value == "S12"
    with pytest.raises(InvalidGroundedAnswer):
        EvidenceId.numbered(0)


def test_evidence_carries_a_logical_uri_and_a_span_but_no_physical_path() -> None:
    evidence = _evidence()

    assert evidence.logical_uri == URI
    assert evidence.source_span.describe() == "lines 18-31"
    assert evidence.location == "lines 18-31"
    fields = set(KnowledgeEvidence.__dataclass_fields__)
    for forbidden in ("physical_path", "absolute_path", "mount_path", "path", "root_path"):
        assert forbidden not in fields


def test_evidence_rejects_blank_content_and_mismatched_roots() -> None:
    with pytest.raises(InvalidGroundedAnswer):
        _evidence(content="   ")
    with pytest.raises(InvalidGroundedAnswer):
        KnowledgeEvidence(
            id=EvidenceId("S1"),
            root_id="other-root",
            entry_id=uuid4(),
            chunk_id=uuid4(),
            logical_uri=URI,
            source_span=SourceSpan.lines(1, 2),
            content="text",
        )


def test_a_page_span_is_described_as_a_page() -> None:
    evidence = KnowledgeEvidence(
        id=EvidenceId("S2"),
        root_id=ROOT_ID,
        entry_id=uuid4(),
        chunk_id=uuid4(),
        logical_uri=StorageUri.parse(f"vault://{ROOT_ID}/report.pdf"),
        source_span=SourceSpan.page(4),
        content="PDF text",
    )

    assert evidence.location == "page 4"


# ------------------------------------------------------------------------ segments


def test_a_segment_needs_text_and_at_least_one_citation() -> None:
    segment = GroundedAnswerSegment(text="  Answer.  ", source_ids=(EvidenceId("S1"),))

    assert segment.text == "Answer."
    with pytest.raises(InvalidGroundedAnswer):
        GroundedAnswerSegment(text="   ", source_ids=(EvidenceId("S1"),))
    with pytest.raises(InvalidGroundedAnswer):
        GroundedAnswerSegment(text="Answer.", source_ids=())
    with pytest.raises(InvalidGroundedAnswer):
        GroundedAnswerSegment(text="x" * 2001, source_ids=(EvidenceId("S1"),))


def test_a_segment_rejects_repeated_or_excessive_citations() -> None:
    with pytest.raises(InvalidGroundedAnswer):
        GroundedAnswerSegment(
            text="x", source_ids=(EvidenceId("S1"), EvidenceId("S1"))
        )
    with pytest.raises(InvalidGroundedAnswer):
        GroundedAnswerSegment(
            text="x",
            source_ids=tuple(EvidenceId.numbered(index) for index in range(1, 10)),
        )


# ------------------------------------------------------------------------- answers


def test_an_answered_result_needs_segments_and_no_reason() -> None:
    answer = GroundedAnswer(
        status=GroundedAnswerStatus.ANSWERED,
        segments=(GroundedAnswerSegment(text="Answer.", source_ids=(EvidenceId("S1"),)),),
    )

    assert answer.cited_source_ids == (EvidenceId("S1"),)
    with pytest.raises(InvalidGroundedAnswer):
        GroundedAnswer(status=GroundedAnswerStatus.ANSWERED)
    with pytest.raises(InvalidGroundedAnswer):
        GroundedAnswer(
            status=GroundedAnswerStatus.ANSWERED,
            segments=(GroundedAnswerSegment("Answer.", (EvidenceId("S1"),)),),
            reason="because",
        )


def test_an_insufficient_result_needs_a_reason_and_no_segments() -> None:
    answer = GroundedAnswer(
        status=GroundedAnswerStatus.INSUFFICIENT_EVIDENCE,
        reason="  The notice does not mention a date.  ",
    )

    assert answer.reason == "The notice does not mention a date."
    with pytest.raises(InvalidGroundedAnswer):
        GroundedAnswer(status=GroundedAnswerStatus.INSUFFICIENT_EVIDENCE)
    with pytest.raises(InvalidGroundedAnswer):
        GroundedAnswer(
            status=GroundedAnswerStatus.INSUFFICIENT_EVIDENCE,
            segments=(GroundedAnswerSegment("Answer.", (EvidenceId("S1"),)),),
            reason="reason",
        )
    with pytest.raises(InvalidGroundedAnswer):
        GroundedAnswer(
            status=GroundedAnswerStatus.INSUFFICIENT_EVIDENCE, reason="x" * 501
        )


def test_there_are_only_two_statuses() -> None:
    assert {status.value for status in GroundedAnswerStatus} == {
        "answered",
        "insufficient_evidence",
    }
    fields = set(GroundedAnswer.__dataclass_fields__)
    for forbidden in ("confidence", "rationale", "reasoning", "score"):
        assert forbidden not in fields


def test_cited_sources_are_unique_and_in_first_appearance_order() -> None:
    answer = GroundedAnswer(
        status=GroundedAnswerStatus.ANSWERED,
        segments=(
            GroundedAnswerSegment("One.", (EvidenceId("S3"), EvidenceId("S1"))),
            GroundedAnswerSegment("Two.", (EvidenceId("S1"), EvidenceId("S2"))),
        ),
    )

    assert answer.cited_source_ids == (
        EvidenceId("S3"),
        EvidenceId("S1"),
        EvidenceId("S2"),
    )


# ----------------------------------------------------------------- citation rules


def test_citations_must_come_from_the_supplied_evidence() -> None:
    answer = GroundedAnswer(
        status=GroundedAnswerStatus.ANSWERED,
        segments=(GroundedAnswerSegment("Answer.", (EvidenceId("S1"),)),),
    )

    validate_citations(answer, frozenset({EvidenceId("S1"), EvidenceId("S2")}))
    with pytest.raises(GroundedAnswerInvalidCitation) as excinfo:
        validate_citations(answer, frozenset({EvidenceId("S2")}))
    assert "S1" in str(excinfo.value)


def test_every_segment_of_a_multi_segment_answer_is_checked() -> None:
    answer = GroundedAnswer(
        status=GroundedAnswerStatus.ANSWERED,
        segments=(
            GroundedAnswerSegment("One.", (EvidenceId("S1"),)),
            GroundedAnswerSegment("Two.", (EvidenceId("S9"),)),
        ),
    )

    with pytest.raises(GroundedAnswerInvalidCitation):
        validate_citations(answer, frozenset({EvidenceId("S1")}))


# -------------------------------------------------------------------------- result


def test_the_result_resolves_citations_locally() -> None:
    first = _evidence("S1")
    second = _evidence("S2", content="The event starts on October 21.")
    answer = GroundedAnswer(
        status=GroundedAnswerStatus.ANSWERED,
        segments=(
            GroundedAnswerSegment("Deadline.", (EvidenceId("S2"), EvidenceId("S1"))),
        ),
    )
    result = GroundedAnswerResult(answer=answer, evidence=(first, second))

    assert [item.id for item in result.cited_evidence()] == [
        EvidenceId("S2"),
        EvidenceId("S1"),
    ]
    assert set(result.evidence_by_id()) == {EvidenceId("S1"), EvidenceId("S2")}


def test_an_insufficient_result_cites_nothing() -> None:
    result = GroundedAnswerResult(
        answer=GroundedAnswer(
            status=GroundedAnswerStatus.INSUFFICIENT_EVIDENCE, reason="Nothing matched."
        ),
        evidence=(_evidence(),),
        offline_roots=("archive-main",),
    )

    assert result.cited_evidence() == ()
    assert result.offline_roots == ("archive-main",)
