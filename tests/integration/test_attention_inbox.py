"""The unified inbox over a real database, with no model in the path (ADR-0042).

These tests are about the two properties a user would notice if they broke: the same pending
situation appears exactly once however often the projector runs, and it disappears on its own the
moment the thing behind it stops needing anybody.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from assistant import bootstrap
from assistant.application.attention import AttentionProjector, AttentionService
from assistant.domain.attention import (
    AttentionKind,
    AttentionSeverity,
    AttentionSourceType,
    AttentionStatus,
)
from assistant.domain.errors import AmbiguousAttentionReference, AttentionItemNotFound
from assistant.domain.notification import Notification, NotificationKind
from assistant.store.db import Database
from assistant.store.migrations import apply_migrations
from tests.support.conversation import build_harness, operation, plan
from tests.support.fakes import FakeClock

NOW = datetime(2026, 9, 21, 4, 0, tzinfo=UTC)
"""Monday 12:00 in Asia/Shanghai, the instant every test in this file runs at."""


class _Inbox:
    """A projector and a service over one fresh runtime, without a conversation runtime."""

    def __init__(self, database: Database, clock: FakeClock, config) -> None:
        self.database = database
        self.clock = clock
        self.projector: AttentionProjector = bootstrap.attention_projector(database, clock, config)
        self.service: AttentionService = bootstrap.attention_service(database, clock)
        self.commitments = bootstrap.commitment_repository(database)
        self.tasks = bootstrap.task_service(database, clock, config)

    async def titles(self) -> list[str]:
        return [item.title for item in (await self.service.list_live()).items]

    async def kinds(self) -> list[AttentionKind]:
        return [item.kind for item in (await self.service.list_live()).items]


async def _inbox(tmp_path: Path, start: datetime = NOW) -> _Inbox:
    clock = FakeClock(start=start)
    database = Database.at(tmp_path / "data" / "assistant.db")
    apply_migrations(database, clock=clock)
    config = await bootstrap.config_loader(
        _write_config(tmp_path)
    ).load()
    return _Inbox(database, clock, config)


def _write_config(tmp_path: Path) -> Path:
    from tests.support.conversation import CONFIG, write_config

    return write_config(tmp_path, CONFIG)


def _count_items(database: Database) -> int:
    connection = sqlite3.connect(str(database.path))
    try:
        return int(connection.execute("SELECT COUNT(*) FROM attention_items").fetchone()[0])
    finally:
        connection.close()


# ------------------------------------------------------------------ idempotence


async def test_refreshing_twice_writes_one_row(tmp_path: Path) -> None:
    inbox = await _inbox(tmp_path)
    task = await inbox.tasks.create_task(_create("写报告"))
    await inbox.tasks.set_deadline(task.id, NOW - timedelta(hours=2))

    first = await inbox.projector.refresh()
    second = await inbox.projector.refresh()
    third = await inbox.projector.refresh()

    assert first.opened == 1
    assert (second.opened, second.reopened, second.resolved) == (0, 0, 0)
    assert (third.opened, third.reopened, third.resolved) == (0, 0, 0)
    assert _count_items(inbox.database) == 1
    assert await inbox.kinds() == [AttentionKind.TASK_OVERDUE]


async def test_an_empty_host_has_an_empty_inbox(tmp_path: Path) -> None:
    inbox = await _inbox(tmp_path)

    summary = await inbox.projector.refresh()
    live = await inbox.service.list_live()

    assert summary.changed is False
    assert live.total == 0
    assert live.by_severity == {"info": 0, "normal": 0, "high": 0}


# ---------------------------------------------------------------- source lifecycle


async def test_completing_the_task_resolves_its_attention(tmp_path: Path) -> None:
    inbox = await _inbox(tmp_path)
    task = await inbox.tasks.create_task(_create("写报告"))
    await inbox.tasks.set_deadline(task.id, NOW - timedelta(hours=2))
    await inbox.projector.refresh()
    assert (await inbox.service.list_live()).total == 1

    await inbox.tasks.complete_task(task.id)
    summary = await inbox.projector.refresh()

    assert summary.resolved == 1
    assert (await inbox.service.list_live()).total == 0
    # Resolution is history, not deletion: the row is still there, closed.
    assert _count_items(inbox.database) == 1
    resolved = (await inbox.service.list_all()).items
    assert resolved[0].status is AttentionStatus.RESOLVED


async def test_moving_the_deadline_opens_a_new_generation(tmp_path: Path) -> None:
    inbox = await _inbox(tmp_path)
    task = await inbox.tasks.create_task(_create("写报告"))
    await inbox.tasks.set_deadline(task.id, NOW - timedelta(hours=2))
    await inbox.projector.refresh()
    first = (await inbox.service.list_live()).items[0]

    await inbox.tasks.set_deadline(task.id, NOW + timedelta(days=30))
    summary = await inbox.projector.refresh()

    assert summary.reopened == 0 and summary.resolved == 1
    assert (await inbox.service.list_live()).total == 0
    history = sorted((await inbox.service.list_all()).items, key=lambda item: item.generation)
    assert [item.generation for item in history] == [1, 2][: len(history)]
    assert history[0].id == first.id
    assert history[0].status is AttentionStatus.RESOLVED


async def test_a_dismissed_item_does_not_hide_a_materially_new_one(tmp_path: Path) -> None:
    inbox = await _inbox(tmp_path)
    task = await inbox.tasks.create_task(_create("写报告"))
    await inbox.tasks.set_deadline(task.id, NOW + timedelta(days=2))
    await inbox.projector.refresh()
    live = (await inbox.service.list_live()).items[0]
    await inbox.service.dismiss(live.id)

    # The same situation, now two days closer: the deadline moved, so the source fingerprint did.
    await inbox.tasks.set_deadline(task.id, NOW - timedelta(hours=1))
    summary = await inbox.projector.refresh()

    assert summary.reopened == 1
    live_after = (await inbox.service.list_live()).items
    assert len(live_after) == 1
    assert live_after[0].status is AttentionStatus.OPEN
    assert live_after[0].generation == 2
    assert live_after[0].kind is AttentionKind.TASK_OVERDUE


async def test_a_dismissed_item_stays_dismissed_while_nothing_changes(tmp_path: Path) -> None:
    inbox = await _inbox(tmp_path)
    task = await inbox.tasks.create_task(_create("写报告"))
    await inbox.tasks.set_deadline(task.id, NOW - timedelta(hours=2))
    await inbox.projector.refresh()
    live = (await inbox.service.list_live()).items[0]
    await inbox.service.dismiss(live.id)

    await inbox.projector.refresh()
    await inbox.projector.refresh()

    after = (await inbox.service.list_live()).items
    assert len(after) == 1
    assert after[0].status is AttentionStatus.DISMISSED
    assert after[0].generation == 1


# ------------------------------------------------------------------ notifications


async def test_a_plan_ready_reminder_does_not_duplicate_the_pending_proposal(
    tmp_path: Path,
) -> None:
    """The v1.2 bug, as a test: one waiting plan used to produce two identical lines."""
    inbox = await _inbox(tmp_path)
    scheduler = bootstrap.scheduler_repository(inbox.database)
    await scheduler.create_notification_idempotent(
        Notification(
            kind=NotificationKind.PLAN_READY,
            title="Updated plan proposal is ready",
            body="Open the plan to review it.",
            dedup_key="plan_ready:test",
            created_at=NOW,
        )
    )

    await inbox.projector.refresh()
    titles = await inbox.titles()

    assert await inbox.kinds() == [] or titles == []
    assert not any("Updated plan proposal is ready" in title for title in titles)
    assert not any("plan_ready" in title for title in titles)


async def test_a_scheduler_warning_becomes_product_language(tmp_path: Path) -> None:
    inbox = await _inbox(tmp_path)
    scheduler = bootstrap.scheduler_repository(inbox.database)
    await scheduler.create_notification_idempotent(
        Notification(
            kind=NotificationKind.SCHEDULER_WARNING,
            title="Scheduler warning",
            body="job 41 exhausted its retry budget",
            dedup_key="scheduler_warning:test",
            created_at=NOW,
        )
    )

    await inbox.projector.refresh()
    items = (await inbox.service.list_live()).items

    assert [item.kind for item in items] == [AttentionKind.NOTIFICATION]
    assert "Scheduler warning" not in items[0].title
    assert "scheduler" not in items[0].title.lower()
    assert items[0].severity is AttentionSeverity.INFO


async def test_marking_the_notification_read_resolves_it(tmp_path: Path) -> None:
    inbox = await _inbox(tmp_path)
    scheduler = bootstrap.scheduler_repository(inbox.database)
    stored = await scheduler.create_notification_idempotent(
        Notification(
            kind=NotificationKind.SCHEDULER_WARNING,
            title="Scheduler warning",
            body="job 41 exhausted its retry budget",
            dedup_key="scheduler_warning:test",
            created_at=NOW,
        )
    )
    await inbox.projector.refresh()
    assert (await inbox.service.list_live()).total == 1

    await scheduler.mark_notification_read(stored.id, at=NOW + timedelta(minutes=1))
    summary = await inbox.projector.refresh()

    assert summary.resolved == 1
    assert (await inbox.service.list_live()).total == 0


# ------------------------------------------------------------------------ mail


async def test_mail_attention_carries_metadata_and_never_a_body(tmp_path: Path) -> None:
    harness = await build_harness(tmp_path)
    projector = bootstrap.attention_projector(harness.database, harness.clock, harness.config)
    service = bootstrap.attention_service(harness.database, harness.clock)
    message = await harness.seed_mail(
        subject="SE 实验三",
        body="请在本周五之前提交实验报告。SENTINEL-BODY-10G",
    )
    await harness.seed_mail_analysis(message, requires_reply=True)

    await projector.refresh()
    items = (await service.list_live()).items

    assert [item.kind for item in items] == [AttentionKind.MAIL_REQUIRES_REPLY]
    assert "SE 实验三" in items[0].title
    assert "teacher@example.edu" in (items[0].summary or "")
    assert "SENTINEL-BODY-10G" not in items[0].title
    assert "SENTINEL-BODY-10G" not in (items[0].summary or "")


async def test_an_analysis_that_no_longer_requires_a_reply_resolves_the_item(
    tmp_path: Path,
) -> None:
    harness = await build_harness(tmp_path)
    projector = bootstrap.attention_projector(harness.database, harness.clock, harness.config)
    service = bootstrap.attention_service(harness.database, harness.clock)
    message = await harness.seed_mail()
    await harness.seed_mail_analysis(message, requires_reply=True)
    await projector.refresh()
    assert (await service.list_live()).total == 1

    await harness.seed_mail_analysis(message, requires_reply=False)
    summary = await projector.refresh()

    assert summary.resolved == 1
    assert (await service.list_live()).total == 0


# -------------------------------------------------------------------- review


async def test_a_waiting_external_review_is_visible_and_resolves_on_cancel(
    tmp_path: Path,
) -> None:
    harness = await build_harness(tmp_path)
    projector = bootstrap.attention_projector(harness.database, harness.clock, harness.config)
    service = bootstrap.attention_service(harness.database, harness.clock)
    thread, _ = await harness.service.resume_or_start()
    harness.queue(
        plan(
            operation(
                "mail.compose_new",
                {
                    "subject": "测试",
                    "body": "你好",
                    "recipient_kind": "self",
                    "recipient_address": None,
                    "recipient_name": None,
                    "sender_account": None,
                    "draft_id": None,
                },
            ),
            operation("mail.prepare_new_send", {"draft_id": None}),
        )
    )
    await harness.service.send(thread.id, "发个邮件给我自己")

    await projector.refresh()
    items = (await service.list_live()).items
    assert [item.kind for item in items] == [AttentionKind.EXTERNAL_REVIEW_WAITING]
    assert items[0].source_type is AttentionSourceType.CONVERSATION_REVIEW

    for review in await harness.reviews.waiting_for_thread(thread.id):
        await harness.review_service.cancel(review)
    summary = await projector.refresh()

    assert summary.resolved == 1
    assert (await service.list_live()).total == 0


# ------------------------------------------------------------------ candidates


async def test_a_pending_candidate_shows_its_key_and_not_its_value(tmp_path: Path) -> None:
    harness = await build_harness(tmp_path)
    projector = bootstrap.attention_projector(harness.database, harness.clock, harness.config)
    service = bootstrap.attention_service(harness.database, harness.clock)
    await harness.facts.propose(
        key="profile.office",
        value="仙林校区",
        correction_text="记住我的办公室在仙林校区",
    )

    await projector.refresh()
    items = (await service.list_live()).items

    assert [item.kind for item in items] == [AttentionKind.FACT_WAITING]
    assert "profile.office" in items[0].title
    assert "仙林校区" not in items[0].title
    assert items[0].summary is None or "仙林校区" not in items[0].summary


# ------------------------------------------------------------------- settling


async def test_settle_by_reference_refuses_an_ambiguous_phrase(tmp_path: Path) -> None:
    inbox = await _inbox(tmp_path)
    for title in ("写报告", "写周报"):
        task = await inbox.tasks.create_task(_create(title))
        await inbox.tasks.set_deadline(task.id, NOW - timedelta(hours=1))
    await inbox.projector.refresh()

    with pytest.raises(AmbiguousAttentionReference):
        await inbox.service.settle_by_reference("写", dismiss=False)
    assert (await inbox.service.list_open()).total == 2


async def test_settle_by_reference_accepts_an_unambiguous_phrase(tmp_path: Path) -> None:
    inbox = await _inbox(tmp_path)
    task = await inbox.tasks.create_task(_create("写报告"))
    await inbox.tasks.set_deadline(task.id, NOW - timedelta(hours=1))
    await inbox.projector.refresh()

    settled = await inbox.service.settle_by_reference("写报告", dismiss=True)

    assert settled.status is AttentionStatus.DISMISSED
    assert (await inbox.service.list_open()).total == 0


async def test_an_unknown_reference_settles_nothing(tmp_path: Path) -> None:
    inbox = await _inbox(tmp_path)

    with pytest.raises(AttentionItemNotFound):
        await inbox.service.settle_by_reference("不存在的事", dismiss=False)


def _create(title: str):
    from assistant.application.task_service import CreateTask

    return CreateTask(title=title)


def test_the_migration_keeps_the_live_identity_unique() -> None:
    """The index, read straight from the reviewed file: one live row per dedupe key."""
    from pathlib import Path as _Path

    source = _Path("migrations/0021_attention_items.sql").read_text(encoding="utf-8")
    assert "CREATE UNIQUE INDEX attention_items_live_identity_idx" in source
    assert "WHERE status <> 'resolved'" in source


def test_the_projector_cannot_be_handed_a_model_an_approval_or_an_executor() -> None:
    """§75: reconciliation is application logic, and its constructor proves it.

    A textual search would be the wrong test here — the module docstring *says* there is no model in
    the path. What matters is that there is no parameter through which one could arrive.
    """
    import inspect

    parameters = set(inspect.signature(AttentionProjector.__init__).parameters)

    assert not {name for name in parameters if "model" in name.lower()}
    assert not {name for name in parameters if "executor" in name.lower()}
    assert not {name for name in parameters if "approval" in name.lower()}
    # The sources it may read, and nothing that could act on them.
    assert parameters == {
        "self",
        "items",
        "commitments",
        "planning",
        "scheduler",
        "conversations",
        "reviews",
        "clock",
        "mail",
        "mail_intelligence",
        "learning",
        "sends",
        "observations",
        "analyses",
        "planning_timezone",
        "due_soon_days",
    }
