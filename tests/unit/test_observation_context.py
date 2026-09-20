"""The bounded, untrusted context a web/manual analysis may see (ADR-0029).

Two properties matter and both are structural. A manual input is quoted JSON with its source name
and nothing else attached — no knowledge, no facts, no tasks, no mail. A page change is a
deterministic diff with budgets, never the whole page, and never a URL a model could try to fetch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.adapters.web_watch.snapshot_store import WebSnapshotStore
from assistant.application.observation_context import (
    ADDED_TEXT_BUDGET,
    CURRENT_EXCERPT_BUDGET,
    MANUAL_TEXT_BUDGET,
    TOTAL_CONTEXT_BUDGET,
    ObservationContextBuilder,
    context_fingerprint,
)
from assistant.domain.manual_input import ManualInput, ManualInputSource
from assistant.domain.web_watch import content_sha256, normalize_text
from assistant.store.db import Database
from assistant.store.migrations import apply_migrations
from assistant.store.web_watch import SqliteWebWatchRepository
from tests.support.fakes import FakeClock
from tests.support.watchers import INJECTION, NOW, TARGET_ID, TARGET_URL


@pytest.fixture
def database(tmp_path: Path) -> Database:
    db = Database.at(tmp_path / "assistant.db")
    apply_migrations(db, clock=FakeClock(start=NOW))
    return db


@pytest.fixture
def snapshot_store(tmp_path: Path) -> WebSnapshotStore:
    return WebSnapshotStore(tmp_path / "runtime")


@pytest.fixture
def builder(
    snapshot_store: WebSnapshotStore, database: Database
) -> ObservationContextBuilder:
    return ObservationContextBuilder(
        snapshot_store, SqliteWebWatchRepository(database)
    )


async def _web_change(
    builder: ObservationContextBuilder,
    store: WebSnapshotStore,
    database: Database,
    *,
    previous: str,
    current: str,
):
    """Store two versions of a page and return the observation of the second one."""
    from assistant.domain.web_watch import WebObservation

    repository = SqliteWebWatchRepository(database)
    first_digest, first_key = await store.store_text(normalize_text(previous))
    second_digest, second_key = await store.store_text(normalize_text(current))
    baseline = WebObservation(
        target_id=TARGET_ID,
        url=TARGET_URL,
        content_sha256=first_digest,
        storage_key=first_key,
        is_baseline=True,
        fetched_at=NOW,
    )
    change = WebObservation(
        target_id=TARGET_ID,
        url=TARGET_URL,
        content_sha256=second_digest,
        storage_key=second_key,
        previous_observation_id=baseline.id,
        fetched_at=NOW,
    )
    await repository.record_observation(
        baseline,
        _state(baseline, first_digest),
    )
    await repository.record_observation(change, _state(change, second_digest))
    return change


def _state(observation, digest: str):
    from assistant.domain.web_watch import WebWatchState

    return WebWatchState(
        target_id=observation.target_id,
        url=observation.url,
        content_sha256=digest,
        latest_observation_id=observation.id,
        updated_at=NOW,
    )


# ---------------------------------------------------------------------- manual input


async def test_a_manual_context_is_the_text_and_its_source(
    builder: ObservationContextBuilder,
) -> None:
    manual_input = ManualInput(
        text="Forwarded: the deadline moved to Friday.",
        source=ManualInputSource.QQ_FORWARD,
        created_at=NOW,
    )

    context = await builder.for_manual(manual_input)

    assert context.payload == {
        "source": "manual.input.received",
        "input_source": "qq-forward",
        "text": manual_input.text,
    }
    assert context.fingerprint == context_fingerprint(context.payload)
    # The identity travels as a source identity for the fingerprint, not inside the quoted data.
    assert f"manual-input:{manual_input.id}" in context.source_identities
    assert manual_input.content_sha256 in context.source_identities


async def test_a_manual_context_carries_nothing_but_the_input(
    builder: ObservationContextBuilder,
) -> None:
    """No knowledge, no facts, no tasks, no mail, no playbooks — only what the user pasted."""
    manual_input = ManualInput(
        text="A short note", source=ManualInputSource.MANUAL, created_at=NOW
    )

    context = await builder.for_manual(manual_input)

    assert set(context.payload) == {"source", "input_source", "text"}


async def test_manual_text_is_bounded(builder: ObservationContextBuilder) -> None:
    manual_input = ManualInput(
        text="x" * 20_000, source=ManualInputSource.MANUAL, created_at=NOW
    )

    context = await builder.for_manual(manual_input)

    assert len(str(context.payload["text"])) <= MANUAL_TEXT_BUDGET + len("\n[truncated]")


# ------------------------------------------------------------------------ web change


async def test_a_page_change_context_is_a_diff_plus_a_bounded_excerpt(
    builder: ObservationContextBuilder,
    snapshot_store: WebSnapshotStore,
    database: Database,
) -> None:
    change = await _web_change(
        builder,
        snapshot_store,
        database,
        previous="Notices\nRegistration closes Oct 20.\n",
        current="Notices\nRegistration closes Oct 25.\nWorkshop Oct 30.\n",
    )

    context = await builder.for_web_change(change)

    assert context.payload["target_id"] == TARGET_ID
    assert context.payload["current_sha256"] == change.content_sha256
    assert "Registration closes Oct 25." in str(context.payload["added_text"])
    assert "Registration closes Oct 20." in str(context.payload["removed_text"])
    assert "Workshop Oct 30." in str(context.payload["current_excerpt"])


async def test_a_page_change_context_never_carries_a_url(
    builder: ObservationContextBuilder,
    snapshot_store: WebSnapshotStore,
    database: Database,
) -> None:
    """A URL in a context is an invitation to fetch it; the target id is enough."""
    change = await _web_change(
        builder, snapshot_store, database, previous="a\n", current="a\nb\n"
    )

    context = await builder.for_web_change(change)

    assert "example.edu" not in context.document
    assert "http" not in context.document
    assert set(context.payload) == {
        "source",
        "target_id",
        "previous_sha256",
        "current_sha256",
        "added_text",
        "removed_text",
        "current_excerpt",
    }


async def test_the_change_context_stays_within_its_budgets(
    builder: ObservationContextBuilder,
    snapshot_store: WebSnapshotStore,
    database: Database,
) -> None:
    change = await _web_change(
        builder,
        snapshot_store,
        database,
        previous="\n".join(f"old line {index}" for index in range(2000)),
        current="\n".join(f"new line {index}" for index in range(2000)),
    )

    context = await builder.for_web_change(change)

    assert len(str(context.payload["added_text"])) <= ADDED_TEXT_BUDGET + len("\n[truncated]")
    assert len(str(context.payload["current_excerpt"])) <= CURRENT_EXCERPT_BUDGET + len(
        "\n[truncated]"
    )
    total = sum(
        len(str(context.payload[field]))
        for field in ("added_text", "removed_text", "current_excerpt")
    )
    assert total <= TOTAL_CONTEXT_BUDGET


async def test_the_diff_is_deterministic(
    builder: ObservationContextBuilder,
    snapshot_store: WebSnapshotStore,
    database: Database,
) -> None:
    change = await _web_change(
        builder, snapshot_store, database, previous="a\nb\n", current="a\nc\n"
    )

    first = await builder.for_web_change(change)
    second = await builder.for_web_change(change)

    assert first.document == second.document
    assert first.fingerprint == second.fingerprint


async def test_a_changed_context_fingerprint_tracks_the_content(
    builder: ObservationContextBuilder,
    snapshot_store: WebSnapshotStore,
    database: Database,
) -> None:
    first = await _web_change(builder, snapshot_store, database, previous="a\n", current="a\nb\n")
    second = await _web_change(builder, snapshot_store, database, previous="a\n", current="a\nc\n")

    assert (
        (await builder.for_web_change(first)).fingerprint
        != (await builder.for_web_change(second)).fingerprint
    )


# --------------------------------------------------------------- prompt injection


async def test_injected_instructions_stay_inside_the_quoted_data(
    builder: ObservationContextBuilder,
) -> None:
    """§43: content is data. It cannot leave the JSON field it is quoted in."""
    manual_input = ManualInput(
        text=INJECTION, source=ManualInputSource.QQ_FORWARD, created_at=NOW
    )

    context = await builder.for_manual(manual_input)
    import json

    decoded = json.loads(context.document)

    assert decoded["text"] == INJECTION.strip()
    assert set(decoded) == {"source", "input_source", "text"}
    assert "http://127.0.0.1" in decoded["text"]  # inside the data field, not outside it
    assert context.document.count("IGNORE ALL PREVIOUS INSTRUCTIONS") == 1


def test_the_stored_hash_is_of_the_normalized_text_not_the_raw_one() -> None:
    assert content_sha256(normalize_text("a  \r\n\r\nb")) == content_sha256(
        normalize_text("a\n\nb")
    )
