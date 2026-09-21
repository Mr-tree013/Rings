"""Attention as a domain object: identity, lifecycle and the closed vocabularies (ADR-0042).

These are the rules the database also enforces. Testing them here as well is deliberate: the
invariants matter to the projector, which builds items in memory before it stores them, and a
constraint that only exists in SQL would let the projector assemble an item no reader could trust.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from assistant.domain.attention import (
    MAX_DEDUPE_KEY_CHARS,
    AttentionItem,
    AttentionKind,
    AttentionSeverity,
    AttentionSourceType,
    AttentionStatus,
    execution_unknown_dedupe_key,
    external_review_dedupe_key,
    fact_waiting_dedupe_key,
    mail_reply_dedupe_key,
    plan_block_dedupe_key,
    plan_waiting_dedupe_key,
    recurring_waiting_dedupe_key,
    sort_key,
    source_fingerprint,
    task_dedupe_key,
    watcher_dedupe_key,
)
from assistant.domain.errors import InvalidAttentionItem

NOW = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)


def _item(**overrides: object) -> AttentionItem:
    values: dict[str, object] = {
        "kind": AttentionKind.TASK_OVERDUE,
        "source_type": AttentionSourceType.TASK,
        "source_id": "task-1",
        "dedupe_key": task_dedupe_key("task-1"),
        "fingerprint": "a" * 64,
        "severity": AttentionSeverity.HIGH,
        "title": "「写报告」已经超过截止时间",
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return AttentionItem(**values)  # type: ignore[arg-type]


def test_a_fresh_item_is_open_and_live() -> None:
    item = _item()

    assert item.status is AttentionStatus.OPEN
    assert item.is_live and item.is_open
    assert item.generation == 1
    assert item.settled_at is None


def test_each_lifecycle_state_owns_exactly_one_timestamp() -> None:
    later = NOW + timedelta(minutes=5)
    acknowledged = _item().acknowledge(later)
    dismissed = _item().dismiss(later)
    resolved = _item().resolve(later)

    assert acknowledged.status is AttentionStatus.ACKNOWLEDGED
    assert (acknowledged.acknowledged_at, acknowledged.dismissed_at) == (later, None)
    assert dismissed.status is AttentionStatus.DISMISSED
    assert (dismissed.dismissed_at, dismissed.acknowledged_at) == (later, None)
    assert resolved.status is AttentionStatus.RESOLVED
    assert (resolved.resolved_at, resolved.acknowledged_at, resolved.dismissed_at) == (
        later,
        None,
        None,
    )
    assert not resolved.is_live


def test_settling_more_than_once_changes_nothing() -> None:
    later = NOW + timedelta(minutes=5)
    acknowledged = _item().acknowledge(later)

    assert acknowledged.acknowledge(later + timedelta(minutes=1)) is acknowledged
    assert acknowledged.dismiss(later + timedelta(minutes=1)) is acknowledged
    assert _item().resolve(later).resolve(later + timedelta(minutes=1)).resolved_at == later


def test_an_inconsistent_lifecycle_is_refused() -> None:
    with pytest.raises(InvalidAttentionItem):
        _item(status=AttentionStatus.ACKNOWLEDGED)
    with pytest.raises(InvalidAttentionItem):
        _item(acknowledged_at=NOW)
    with pytest.raises(InvalidAttentionItem):
        _item(status=AttentionStatus.DISMISSED, acknowledged_at=NOW, dismissed_at=NOW)


def test_bounds_and_fingerprint_are_enforced() -> None:
    with pytest.raises(InvalidAttentionItem):
        _item(fingerprint="a" * 63)
    with pytest.raises(InvalidAttentionItem):
        _item(fingerprint="A" * 64)
    with pytest.raises(InvalidAttentionItem):
        _item(title="")
    with pytest.raises(InvalidAttentionItem):
        _item(dedupe_key="x" * (MAX_DEDUPE_KEY_CHARS + 1))
    with pytest.raises(InvalidAttentionItem):
        _item(summary="x" * 501)
    with pytest.raises(InvalidAttentionItem):
        _item(generation=0)
    with pytest.raises(InvalidAttentionItem):
        _item(updated_at=NOW - timedelta(seconds=1))
    with pytest.raises(InvalidAttentionItem):
        _item(created_at=datetime(2026, 9, 22, 10, 0))


def test_the_fingerprint_is_a_canonical_sha256() -> None:
    first = source_fingerprint({"b": 2, "a": 1})
    second = source_fingerprint({"a": 1, "b": 2})

    assert first == second
    assert len(first) == 64
    assert source_fingerprint({"a": 1}) != first


def test_a_new_generation_is_a_new_identity_with_the_same_dedupe_key() -> None:
    first = _item()
    second = first.next_generation(
        kind=AttentionKind.TASK_DUE_SOON,
        fingerprint="b" * 64,
        severity=AttentionSeverity.NORMAL,
        title="「写报告」临近截止",
        summary=None,
        at=NOW + timedelta(hours=1),
    )

    assert second.dedupe_key == first.dedupe_key
    assert second.id != first.id
    assert second.generation == 2
    assert second.status is AttentionStatus.OPEN
    assert second.kind is AttentionKind.TASK_DUE_SOON


def test_severity_orders_high_then_normal_then_info() -> None:
    later = NOW + timedelta(days=3)
    high = _item(severity=AttentionSeverity.HIGH, created_at=later, updated_at=later)
    normal = _item(severity=AttentionSeverity.NORMAL)
    info = _item(severity=AttentionSeverity.INFO)

    ordered = sorted([info, normal, high], key=sort_key)

    assert [item.severity for item in ordered] == [
        AttentionSeverity.HIGH,
        AttentionSeverity.NORMAL,
        AttentionSeverity.INFO,
    ]


def test_dedupe_keys_are_stable_and_name_the_source() -> None:
    identifier = uuid4()
    pairs = (
        (task_dedupe_key(identifier), f"task-overdue:{identifier}"),
        (plan_block_dedupe_key(identifier), f"plan-passed:{identifier}"),
        (mail_reply_dedupe_key(identifier), f"mail-reply:{identifier}"),
        (plan_waiting_dedupe_key(identifier), f"plan-waiting:{identifier}"),
        (fact_waiting_dedupe_key(identifier), f"fact-waiting:{identifier}"),
        (recurring_waiting_dedupe_key(identifier), f"recurring-waiting:{identifier}"),
        (external_review_dedupe_key(identifier), f"external-review:{identifier}"),
        (execution_unknown_dedupe_key(identifier), f"execution-unknown:{identifier}"),
        (watcher_dedupe_key(identifier), f"watcher-observation:{identifier}"),
    )
    for built, expected in pairs:
        assert built == expected
    assert len({built for built, _ in pairs}) == len(pairs)


def test_the_payload_carries_product_words_and_no_source_internals() -> None:
    payload = _item(summary="截止时间是 09-22 10:00。").to_payload()

    assert payload["kind"] == "task_overdue"
    assert payload["severity"] == "high"
    assert payload["status"] == "open"
    assert payload["generation"] == 1
    assert "fingerprint" not in payload
