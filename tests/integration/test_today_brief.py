"""The deterministic today brief (Phase 10G, ADR-0039).

Real services over a real database, with no model in the path at all: the brief is a read of
existing state, bounded, ordered and rendered by the runtime. The tests below pin the sources it
uses, the timezone that defines "today", what it refuses to say, and the fact that building one
changes nothing.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from assistant import bootstrap
from assistant.application.calendar_service import CreateCalendarEvent
from assistant.application.today_brief import (
    ATTENTION_BRIEF_LIMIT,
    CALENDAR_LIMIT,
    TodayBriefService,
)
from assistant.domain.config import AssistantConfig
from assistant.domain.errors import PlanningNotConfigured
from assistant.store.db import Database
from assistant.store.migrations import apply_migrations
from tests.support.conversation import CONFIG, build_harness, operation, plan
from tests.support.fakes import FakeClock

MONDAY = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)
"""Monday 08:00 in Asia/Shanghai: the fixed instant every brief test runs at."""

CLASS_TITLE = "计算机系统基础课"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=MONDAY)


def _service(
    database: Database,
    clock: FakeClock,
    config: AssistantConfig | None,
) -> TodayBriefService:
    return bootstrap.today_brief_service(database, clock, config)


async def _config(tmp_path: Path, body: str = CONFIG) -> AssistantConfig:
    from tests.support.conversation import write_config

    return await bootstrap.config_loader(write_config(tmp_path, body)).load()


def _counts(database: Database) -> dict[str, int]:
    connection = sqlite3.connect(str(database.path))
    try:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "tasks",
                "calendar_events",
                "plan_blocks",
                "recurring_calendar_rules",
                "notifications",
                "mail_messages",
                "action_requests",
                "approvals",
                "execution_runs",
                "fact_candidates",
                "confirmed_facts",
            )
        }
    finally:
        connection.close()


# ------------------------------------------------------------------- timezone (§2)


async def test_today_is_the_planning_timezone_local_day(tmp_path: Path) -> None:
    """Monday 00:00 UTC is Monday 08:00 in Asia/Shanghai — the same day, but the *user's* day."""
    clock = FakeClock(start=MONDAY)
    database = Database.at(tmp_path / "assistant.db")
    apply_migrations(database, clock=clock)
    config = await _config(tmp_path)

    service = _service(database, clock, config)
    start, end = service.window()

    assert service.today().isoformat() == "2026-09-21"
    assert start == datetime(2026, 9, 20, 16, 0, tzinfo=UTC)  # local midnight, as an instant
    assert end - start == timedelta(days=1)


async def test_a_host_timezone_cannot_change_the_brief(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(start=MONDAY)
    database = Database.at(tmp_path / "assistant.db")
    apply_migrations(database, clock=clock)
    config = await _config(tmp_path)
    observed: list[str] = []

    for host_zone in ("UTC", "America/New_York", "Asia/Tokyo"):
        monkeypatch.setenv("TZ", host_zone)
        brief = await _service(database, clock, config).build()
        observed.append(f"{brief.local_date.isoformat()}@{brief.timezone}")

    assert observed == ["2026-09-21@Asia/Shanghai"] * 3


async def test_without_a_planning_timezone_today_is_unanswerable(tmp_path: Path) -> None:
    from tests.support.conversation import CONFIG_WITHOUT_TIMEZONE

    clock = FakeClock(start=MONDAY)
    database = Database.at(tmp_path / "assistant.db")
    apply_migrations(database, clock=clock)
    config = await _config(tmp_path, CONFIG_WITHOUT_TIMEZONE)

    with pytest.raises(PlanningNotConfigured):
        await _service(database, clock, config).build()


# ---------------------------------------------------------------- conversation (§4-§5)


async def test_a_brief_renders_without_a_model_and_without_mutating(tmp_path: Path) -> None:
    harness = await build_harness(tmp_path)
    task = await harness.create_task("写完 CSAPP 第 6 章", estimated_minutes=120)
    await harness.tasks.set_deadline(task.id, datetime(2026, 9, 21, 6, 0, tzinfo=UTC))
    thread, _ = await harness.service.resume_or_start()
    harness.queue(plan(operation("brief.today", {})))
    before = _counts(harness.database)
    calls_before = len(harness.model.requests)

    reply = await harness.service.send(thread.id, "我今天有什么事？")

    assert "今天（2026-09-21" in reply.text
    assert "Asia/Shanghai" in reply.text
    # The interpretation is the only provider call; the brief itself is deterministic.
    assert len(harness.model.requests) == calls_before + 1
    assert _counts(harness.database) == before


async def test_the_brief_is_not_interpretted_when_no_timezone_is_configured(
    tmp_path: Path,
) -> None:
    from tests.support.conversation import CONFIG_WITHOUT_TIMEZONE

    harness = await build_harness(tmp_path, config_body=CONFIG_WITHOUT_TIMEZONE)
    thread, _ = await harness.service.resume_or_start()
    harness.queue(plan(operation("brief.today", {})))

    reply = await harness.service.send(thread.id, "我今天有什么事？")

    assert "时区" in reply.text
    assert _counts(harness.database)["tasks"] == 0


# ------------------------------------------------------------------- sources (§3)


async def test_the_brief_shows_todays_schedule(tmp_path: Path) -> None:
    harness = await build_harness(tmp_path)
    await harness.calendar.create_event(
        CreateCalendarEvent(
            title="小组会",
            starts_at=datetime(2026, 9, 21, 2, 0, tzinfo=UTC),
            ends_at=datetime(2026, 9, 21, 3, 0, tzinfo=UTC),
        )
    )
    await harness.recurring.create_weekly(
        title=CLASS_TITLE, weekday=1, start="10:00", end="12:00"
    )
    brief = await harness.today_brief.build()

    labels = [(entry.kind, entry.label) for entry in brief.schedule]

    assert ("calendar_event", "小组会") in labels
    assert ("recurring", CLASS_TITLE) in labels
    assert len(brief.schedule) == 2


async def test_the_brief_shows_applied_plan_blocks_by_task_title(tmp_path: Path) -> None:
    from assistant.application.calendar_service import CreatePlanBlock

    harness = await build_harness(tmp_path)
    task = await harness.create_task("写 SE 报告", estimated_minutes=120)
    await harness.calendar.create_plan_block(
        CreatePlanBlock(
            task_id=task.id,
            starts_at=datetime(2026, 9, 21, 11, 0, tzinfo=UTC),
            ends_at=datetime(2026, 9, 21, 12, 0, tzinfo=UTC),
        )
    )

    brief = await harness.today_brief.build()

    assert [entry.label for entry in brief.schedule if entry.kind == "plan_block"] == [
        "写 SE 报告"
    ]


async def test_the_brief_shows_overdue_and_due_today_tasks(tmp_path: Path) -> None:
    harness = await build_harness(tmp_path)
    overdue = await harness.create_task("交实验报告")
    await harness.tasks.set_deadline(overdue.id, MONDAY - timedelta(hours=1))
    due_today = await harness.create_task("读完 CSAPP 第 6 章")
    await harness.tasks.set_deadline(due_today.id, datetime(2026, 9, 21, 6, 0, tzinfo=UTC))
    soon = await harness.create_task("准备答辩")
    await harness.tasks.set_deadline(soon.id, MONDAY + timedelta(days=2))

    brief = await harness.today_brief.build()

    labels = [entry.label for entry in brief.tasks]
    assert labels == ["交实验报告", "读完 CSAPP 第 6 章", "准备答辩"]
    details = {entry.label: entry.detail for entry in brief.tasks}
    assert "已超过截止时间" in str(details["交实验报告"])
    assert "今天 14:00 截止" in str(details["读完 CSAPP 第 6 章"])


async def test_the_brief_bounds_every_section(tmp_path: Path) -> None:
    harness = await build_harness(tmp_path)
    for index in range(CALENDAR_LIMIT + 3):
        await harness.calendar.create_event(
            CreateCalendarEvent(
                title=f"会议 {index}",
                starts_at=datetime(2026, 9, 21, 1, 0, tzinfo=UTC)
                + timedelta(minutes=10 * index),
                ends_at=datetime(2026, 9, 21, 1, 5, tzinfo=UTC)
                + timedelta(minutes=10 * index),
            )
        )

    brief = await harness.today_brief.build()

    assert len(brief.schedule) == CALENDAR_LIMIT
    assert brief.overflow["schedule"] == 3


# ------------------------------------------------------------------ attention (§3, §7)


async def test_the_brief_shows_attention_items_without_mail_bodies(tmp_path: Path) -> None:
    harness = await build_harness(tmp_path)
    message = await harness.seed_mail(
        subject="SE 实验三", body="这是邮件正文的哨兵 SENTINEL-BODY-10G。"
    )
    await harness.seed_mail_analysis(message, requires_reply=True)
    harness.queue(plan(operation("brief.today", {})))
    thread, _ = await harness.service.resume_or_start()

    reply = await harness.service.send(thread.id, "最近有什么需要我处理的？")

    assert "需要回复" in reply.text
    assert "SE 实验三" in reply.text
    assert "SENTINEL-BODY-10G" not in reply.text


async def test_the_brief_bounds_mail_attention(tmp_path: Path) -> None:
    harness = await build_harness(tmp_path)
    for index in range(ATTENTION_BRIEF_LIMIT + 2):
        message = await harness.seed_mail(
            subject=f"邮件 {index}", uid=index + 1, message_id=f"m{index}@example.edu"
        )
        await harness.seed_mail_analysis(message, requires_reply=True)

    brief = await harness.today_brief.build()

    # Phase 11B: one normalised inbox line per pending message, bounded by the brief's own limit and
    # counted once, instead of a second, differently-bounded mail section beside it.
    attention_items = [entry for entry in brief.attention if entry.kind == "attention"]
    assert len(attention_items) == ATTENTION_BRIEF_LIMIT
    # The inbox service reads one row past the bound, so the brief can say truthfully that there is
    # more without reading the whole list to count it exactly.
    assert brief.overflow["attention"] >= 1


async def test_the_brief_shows_waiting_items_without_executing_them(
    tmp_path: Path,
) -> None:
    harness = await build_harness(tmp_path)
    await harness.facts.propose(
        key="profile.office",
        value="仙林校区",
        correction_text="记住我的办公室在仙林校区",
    )
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
    await harness.service.send(thread.id, "发个打招呼的邮件给我自己")
    before = _counts(harness.database)

    brief = await harness.today_brief.build()

    attention = " ".join(entry.label for entry in brief.attention)
    assert "等待你确认" in attention
    assert "profile.office" in attention, "the key is what tells the user which proposal is waiting"
    assert "仙林校区" not in attention, (
        "a candidate's value is never copied into an inbox line — only its key (ADR-0042 §15)"
    )
    waiting = " ".join(entry.label for entry in brief.waiting)
    assert "邮件等待你的发送确认" in waiting
    # Building the brief neither sends nor approves anything.
    assert harness.executor.calls == []
    assert _counts(harness.database)["approvals"] == before["approvals"] == 0
    assert _counts(harness.database)["execution_runs"] == 0


async def test_an_unknown_send_is_reported_as_unknown_not_failed(
    tmp_path: Path,
) -> None:
    from assistant.domain.execution import ExecutionOutcome
    from tests.support.conversation import ScriptedMailExecutor

    executor = ScriptedMailExecutor(ExecutionOutcome.unknown("connection dropped"))
    harness = await build_harness(tmp_path, executor=executor)
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
    await harness.service.send(thread.id, "发个打招呼的邮件给我自己")
    await harness.service.send(thread.id, "确认发送")
    assert len(executor.calls) == 1

    harness.queue(plan(operation("brief.today", {})))
    reply = await harness.service.send(thread.id, "我今天有什么事？")

    assert "不确定" in reply.text
    assert "不会自动重试" in reply.text
    assert "失败" not in reply.text
    assert len(executor.calls) == 1


async def test_a_pending_plan_proposal_is_visible(tmp_path: Path) -> None:
    harness = await build_harness(tmp_path)
    await harness.create_task("写报告", estimated_minutes=120)
    thread, _ = await harness.service.resume_or_start()
    harness.queue(plan(operation("plan.propose_week", {"next_week": True})))
    proposed = await harness.service.send(thread.id, "帮我安排下周")
    assert proposed.waiting_for_confirmation is True

    brief = await harness.today_brief.build()

    # Phase 11B: the pending proposal is reported once, as one normalised attention line, rather
    # than once here and once as a `plan_ready` reminder with an internal title.
    labels = " ".join(entry.label for entry in brief.attention)
    assert "周计划" in labels
    assert "plan_ready" not in labels
    assert "Updated plan proposal is ready" not in labels
    assert _counts(harness.database)["plan_blocks"] == 0


async def test_an_empty_day_says_so(tmp_path: Path) -> None:
    harness = await build_harness(tmp_path)
    thread, _ = await harness.service.resume_or_start()
    harness.queue(plan(operation("brief.today", {})))

    reply = await harness.service.send(thread.id, "我 今天有什么事？")

    assert "暂时没有安排" in reply.text
