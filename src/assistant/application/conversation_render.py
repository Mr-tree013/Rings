"""Turning operation results into the words the user reads (ADR-0033 §19).

The runtime writes this text, not the model. That is the whole point of the module: for an
operations turn the model's own `reply` is discarded, so a turn can never narrate an effect that
did not happen — the sentences below are produced from the same values the handlers just wrote to
the database.

Every line is deterministic: the same result renders identically, timestamps are shown in the
user's planning timezone (falling back to UTC when none is configured), and nothing here reaches
back into a service.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from assistant.application.conversation_capabilities.registry import OperationResult
from assistant.domain.task import TaskPriority

CONFIRMATION_OFFER = (
    "要应用这个计划吗？回复「可以」我就写进计划，或回复「取消」。"
)
"""Shown after a proposal is created; the confirmation itself is a durable pending operation."""

PRIORITY_LABELS: dict[str, str] = {
    TaskPriority.HIGH.value: "高",
    TaskPriority.NORMAL.value: "普通",
    TaskPriority.LOW.value: "低",
}


def render_results(
    results: Sequence[OperationResult], *, timezone: str | None
) -> str:
    """Render one or more successful operations as the assistant's message."""
    return "\n".join(
        block
        for result in results
        for block in [render_result(result, timezone=timezone)]
        if block
    )


def render_result(result: OperationResult, *, timezone: str | None) -> str:
    """Render one operation result."""
    data = result.data
    if result.kind == "status":
        return _render_status(data, timezone)
    if result.kind == "tasks":
        return _render_tasks(data, timezone)
    if result.kind in ("task", "task_updated"):
        prefix = "已更新任务" if result.kind == "task_updated" else ""
        return _render_task(data, timezone, prefix=prefix)
    if result.kind == "task_created":
        return _render_task(data, timezone, prefix="已创建任务")
    if result.kind == "task_completed":
        return f"已完成任务「{data.get('title', '')}」。"
    if result.kind == "deadline_set":
        return (
            f"已把「{data.get('title', '')}」的截止时间设为 "
            f"{_moment(data.get('due_at'), timezone)}。"
        )
    if result.kind == "deadline_cleared":
        return f"已清除「{data.get('title', '')}」的截止时间。"
    if result.kind == "calendar":
        return _render_calendar(data, timezone)
    if result.kind == "event_created":
        starts = _moment(data.get("starts_at"), timezone)
        ends = _moment(data.get("ends_at"), timezone)
        return (
            f"已记录日程「{data.get('title', '')}」："
            f"{starts} → {ends}。"
        )
    if result.kind == "work_recorded":
        minutes = data.get("minutes", 0)
        started = _moment(data.get("started_at"), timezone)
        ended = _moment(data.get("ended_at"), timezone)
        return (
            f"已记录「{data.get('title', '')}」的工作时间：{minutes} 分钟"
            f"（{started} → {ended}）。"
        )
    if result.kind in ("proposal", "proposal_applied"):
        return _render_proposal(data, timezone, result.kind == "proposal_applied")
    if result.kind == "proposal_absent":
        return "现在没有待审阅的周计划提案。"
    if result.kind == "notifications":
        return _render_notifications(data, timezone)
    if result.kind == "notification_read":
        return f"已把提醒「{data.get('title', '')}」标记为已读。"
    if result.kind == "answer":
        return _render_answer(data)
    raise AssertionError(f"unrendered operation result: {result.kind}")  # pragma: no cover


def render_failure(operation_type: str, message: str) -> str:
    """One failed operation, in the user's words."""
    return f"「{operation_type}」没有完成：{message}"


def render_unsupported(operation_types: Sequence[str]) -> str:
    """A plan that asked for something this build does not offer."""
    asked = "、".join(sorted(set(operation_types)))
    return (
        f"你要求的操作（{asked}）不在这一版 Tree 能做的范围内。"
        "目前我只能处理本地的任务、日历、工作记录、周计划和资料问答；"
        "从对话里发邮件或提交校外系统的手续会在后续版本接入。"
    )


def render_interpretation_refused(reason: str) -> str:
    """The model answered with something the runtime will not act on."""
    return f"我没能安全地理解这条请求，所以什么都没有执行。（{reason}）"


def render_confirmation_request(operation_type: str) -> str:
    """Ask for the one confirmation a `CONFIRM_LOCAL` operation needs."""
    return (
        f"这一步需要你确认：{operation_type}。"
        "回复「可以」我就执行，或回复「取消」。"
    )


def render_confirmation_expired() -> str:
    """A pending confirmation was answered too late (ADR-0033 §12)."""
    return (
        "这个确认已经过期了。"
        "请重新提出一次请求，我会再准备一份新的提案。"
    )


def render_confirmation_rejected() -> str:
    """The user said no."""
    return "好，这个计划没有应用。需要的时候再说一声。"


def render_multiple_pending(listing: Sequence[str]) -> str:
    """More than one pending confirmation: the runtime asks, and never guesses."""
    joined = "\n".join(f"- {entry}" for entry in listing)
    return f"现在有多个待确认的操作，请告诉我是哪一个：\n{joined}"


def render_empty_turn() -> str:
    """A turn whose own words were empty still answers with something."""
    return "（没有需要回复的内容。）"


def render_unknown_local() -> str:
    """The crash fence: an interrupted local write."""
    return (
        "上一次的本地写入在完成前中断了，我无法确认它是否已经生效。"
        "请先检查当前状态，再决定要不要重试；我不会自动重放它。"
    )


def _render_status(data: dict[str, Any], timezone: str | None) -> str:
    lines = [f"当前有 {data.get('open_tasks', 0)} 个未完成任务。"]
    next_deadline = data.get("next_deadline")
    if isinstance(next_deadline, dict):
        lines.append(
            f"最近的一个是「{next_deadline.get('title', '')}」，"
            f"截止 {_moment(next_deadline.get('due_at'), timezone)}。"
        )
    else:
        lines.append("目前没有任何截止时间。")
    unread = int(data.get("unread_notifications", 0))
    lines.append("没有未读提醒。" if unread == 0 else f"还有 {unread} 条未读提醒。")
    if data.get("pending_proposal") is None:
        lines.append("没有待审阅的周计划提案。")
    else:
        identifier = _short(data.get("pending_proposal"))
        lines.append(f"有一份待审阅的周计划提案（{identifier}）。")
    return "\n".join(lines)


def _render_tasks(data: dict[str, Any], timezone: str | None) -> str:
    tasks = data.get("tasks") or []
    if not tasks:
        return "现在没有符合条件的任务。"
    lines = [f"共 {len(tasks)} 个任务："]
    for task in tasks:
        lines.append(f"- {_task_line(task, timezone)}")
    return "\n".join(lines)


def _render_task(data: dict[str, Any], timezone: str | None, *, prefix: str) -> str:
    task = data.get("task") or {}
    title = task.get("title", "")
    identifier = _short(task.get("id"))
    head = (
        f"{prefix}「{title}」（{identifier}）。"
        if prefix
        else f"任务「{title}」（{identifier}）："
    )
    details = [_task_line(task, timezone)]
    if task.get("description"):
        details.append(f"说明：{task['description']}")
    return "\n".join([head, *details])


def _render_calendar(data: dict[str, Any], timezone: str | None) -> str:
    events = data.get("events") or []
    blocks = data.get("plan_blocks") or []
    if not events and not blocks:
        return f"接下来 {data.get('window_days', 0)} 天没有已记录的日程或计划。"
    lines = [f"接下来 {data.get('window_days', 0)} 天："]
    for event in events:
        lines.append(
            f"- 日程「{event.get('title', '')}」"
            f"（{_moment(event.get('starts_at'), timezone)} → "
            f"{_moment(event.get('ends_at'), timezone)}）"
        )
    for block in blocks:
        label = block.get("title", "")
        lines.append(
            f"- 计划块「{label}」"
            f"（{_moment(block.get('starts_at'), timezone)} → "
            f"{_moment(block.get('ends_at'), timezone)}）"
        )
    return "\n".join(lines)


def _render_proposal(data: dict[str, Any], timezone: str | None, applied: bool) -> str:
    identifier = _short(data.get("id"))
    if applied:
        head = (
            f"已应用周计划提案（{identifier}）："
            f"新增 {data.get('created_blocks', 0)} 个时间块，"
            f"替换 {data.get('replaced_blocks', 0)} 个。"
        )
    else:
        head = (
            f"周计划提案 {identifier}（{data.get('window_starts_at', '')[:10]} 起）："
            f"提出 {data.get('block_count', 0)} 个时间块，"
            f"有 {data.get('issue_count', 0)} 条提示。"
        )
    lines = [head]
    for block in data.get("blocks") or []:
        lines.append(
            f"- {_moment(block.get('starts_at'), timezone)} → "
            f"{_moment(block.get('ends_at'), timezone)}"
        )
    for issue in data.get("issues") or []:
        lines.append(f"- 提示 {issue.get('code', '')}：{issue.get('message', '')}")
    if not applied:
        lines.append(CONFIRMATION_OFFER)
    return "\n".join(lines)


def _render_notifications(data: dict[str, Any], timezone: str | None) -> str:
    notifications = data.get("notifications") or []
    if not notifications:
        return "提醒收件箱是空的。"
    lines = [f"共 {len(notifications)} 条提醒："]
    for notification in notifications:
        lines.append(
            f"- [{_short(notification.get('id'))}] {notification.get('title', '')}"
            f"（{notification.get('kind', '')}，"
            f"{_moment(notification.get('created_at'), timezone)}）"
        )
    return "\n".join(lines)


def _render_answer(data: dict[str, Any]) -> str:
    if data.get("status") != "answered":
        return (
            "我在你索引的资料里没有找到能回答这个问题的依据，"
            "所以不给你一个没有出处的答案。"
        )
    body = "".join(
        f"{segment.get('text', '')} {_citations(segment)}"
        for segment in data.get("segments") or []
    )
    sources = data.get("sources") or []
    lines = [body.strip()]
    if sources:
        lines.append("来源：")
        for source in sources:
            identifier = source.get("id", "")
            lines.append(
                f"- [{identifier}] {source.get('label', '')} - {source.get('span', '')}"
            )
    for offline in data.get("offline_roots") or []:
        lines.append(f"- （{offline} 当前离线，未纳入这次回答）")
    return "\n".join(lines)


def _task_line(task: dict[str, Any], timezone: str | None) -> str:
    parts = [
        f"[{_short(task.get('id'))}] {task.get('title', '')}",
        f"状态 {task.get('status', '')}",
        f"优先级 {_priority_label(task.get('priority'))}",
    ]
    if task.get("due_at"):
        parts.append(f"截止 {_moment(task.get('due_at'), timezone)}")
    if task.get("estimated_minutes") is not None:
        parts.append(f"预计 {task['estimated_minutes']} 分钟")
    seconds = int(task.get("actual_seconds") or 0)
    if seconds:
        parts.append(f"已用 {seconds // 60} 分钟")
    return " · ".join(str(part) for part in parts)


def _short(identifier: object) -> str:
    text = "" if identifier is None else str(identifier)
    return text[:8]


def _priority_label(value: object) -> str:
    """A priority is a known enum value; anything else is echoed rather than guessed at."""
    text = "" if value is None else str(value)
    return PRIORITY_LABELS.get(text, text)


def _citations(segment: dict[str, Any]) -> str:
    """The `[S1] [S2]` markers of one answer segment."""
    return " ".join(f"[{source}]" for source in segment.get("sources") or [])


def _moment(value: object, timezone: str | None) -> str:
    """Render an instant in the user's planning timezone, or UTC."""
    if value is None:
        return "未设置"
    try:
        instant = datetime.fromisoformat(str(value))
    except ValueError:  # pragma: no cover - every caller passes an ISO string it produced
        return str(value)
    zone = _zone(timezone)
    localised = instant.astimezone(zone)
    suffix = "UTC" if zone is UTC else _offset_text(localised.utcoffset())
    return f"{localised.strftime('%Y-%m-%d %H:%M')}（{suffix}）"


def _offset_text(offset: timedelta | None) -> str:
    """Render a UTC offset the way a person writes one: `+08:00`, `-04:00`, `+05:30`."""
    if offset is None:  # pragma: no cover - an aware instant always has one
        return "UTC"
    minutes = int(offset.total_seconds()) // 60
    sign = "+" if minutes >= 0 else "-"
    hours, remainder = divmod(abs(minutes), 60)
    return f"{sign}{hours:02d}:{remainder:02d}"


def _zone(timezone: str | None) -> ZoneInfo | Any:
    if not timezone:
        return UTC
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):  # pragma: no cover - config validates this
        return UTC


__all__ = [
    "CONFIRMATION_OFFER",
    "render_confirmation_expired",
    "render_confirmation_rejected",
    "render_confirmation_request",
    "render_empty_turn",
    "render_failure",
    "render_interpretation_refused",
    "render_multiple_pending",
    "render_result",
    "render_results",
    "render_unknown_local",
    "render_unsupported",
]
