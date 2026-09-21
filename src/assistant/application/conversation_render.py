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
from assistant.domain.conversation_errors import ConversationErrorCode
from assistant.domain.recurring_calendar import weekday_label
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
    if result.kind == "recurring_rules":
        return _render_recurring_rules(data)
    if result.kind == "recurring_rule_created":
        return _render_recurring_created(data)
    if result.kind == "recurring_rule_updated":
        return _render_recurring_updated(data)
    if result.kind == "recurring_rule_retired":
        return _render_recurring_retired(data)
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
    if result.kind == "mail_status":
        return _render_mail_status(data)
    if result.kind == "mail_synced":
        return _render_mail_synced(data)
    if result.kind == "mail_messages":
        return _render_mail_messages(data, timezone)
    if result.kind == "mail_message":
        return _render_mail_message(data, timezone)
    if result.kind == "mail_thread":
        return _render_mail_thread(data, timezone)
    if result.kind == "mail_draft":
        return _render_mail_draft(data)
    if result.kind == "mail_prepared":
        # The preview is rendered by the service from the immutable ActionRequest payload; this
        # path only exists so an unexpected direct render still says something true.
        return "已准备好这封邮件，正在给你确认预览。"
    if result.kind == "mail_reconciled":
        return _render_mail_reconciled(data)
    if result.kind == "mail_reconciliation_absent":
        return "还没有准备过要发送的邮件，所以没有可以对账的对象。"
    if result.kind == "mail_accounts":
        return _render_mail_accounts(data)
    if result.kind == "capabilities":
        return render_capabilities(data)
    raise AssertionError(f"unrendered operation result: {result.kind}")  # pragma: no cover


_STATE_WORDS = {
    "available": "可用",
    "not_configured": "未配置",
    "disabled": "已关闭",
    "unavailable": "本版本不支持",
}


def _render_mail_accounts(data: dict[str, Any]) -> str:
    """Configured mailboxes — never confused with how much mail happens to be stored."""
    accounts = data.get("accounts") or []
    if not accounts:
        return "当前没有配置任何邮箱，所以我不能读取或发送邮件。"
    lines = [f"当前配置了 {len(accounts)} 个邮箱："]
    for account in accounts:
        address = account.get("from_address") or account.get("username") or "（未设置地址）"
        readiness = []
        readiness.append("收信已配置" if account.get("receive_configured") else "收信未配置")
        readiness.append("发信已配置" if account.get("send_configured") else "发信未配置")
        if not account.get("enabled"):
            readiness.append("已停用")
        lines.append(
            f"- {account.get('account_id', '')} — {address}"
            f"（IMAP {account.get('host', '')}；{'、'.join(readiness)}）"
        )
    cached = sum(int(account.get("stored_messages", 0)) for account in accounts)
    lines.append(f"本地目前缓存了 {cached} 封邮件。")
    return "\n".join(lines)


def render_capabilities(data: dict[str, Any]) -> str:
    """What Tree can do here, straight from the runtime's own metadata."""
    areas = data.get("areas") or {}

    def state_of(name: str) -> str:
        area = areas.get(name) or {}
        return str(area.get("state", "unavailable"))

    lines = ["我现在能做这些（按当前配置）："]
    lines.append("· 任务：查看列表和详情、新建、改标题/优先级/预估、设或清截止时间、标记完成")
    lines.append("· 日历与工作记录：查看未来几天的安排、登记已占用的时间、记录实际投入")
    recurring = areas.get("recurring_calendar") or {}
    if state_of("recurring_calendar") == "available":
        lines.append(
            "· 固定安排（每周重复）：查看、新增、修改、停用；一条规则一个星期几，"
            "可以设结束日期"
        )
        lines.append("· 周计划：自动避开固定安排占用的时间，不会把任务排在课上")
    else:
        lines.append("· 固定安排：需要先在配置里设置 [planning].timezone")
    lines.append("· 提醒：查看提醒收件箱、把某条标为已读")
    planning = areas.get("planning") or {}
    if state_of("planning") == "available":
        timezone = planning.get("timezone") or "未设置"
        lines.append(f"· 周计划：生成并应用周计划提案（时区 {timezone}；应用前需要你确认）")
    else:
        lines.append("· 周计划：需要先在配置里设置 [planning].timezone")
    knowledge = areas.get("knowledge") or {}
    if state_of("knowledge") == "available":
        lines.append(
            f"· 资料问答：从你索引的 {knowledge.get('configured_roots', 0)} 个目录里提问并给出引用"
        )
    else:
        lines.append("· 资料问答：还没有配置要索引的目录")
    mail_read = areas.get("mail_read") or {}
    if state_of("mail_read") == "available":
        lines.append(
            f"· 邮件（已配置 {mail_read.get('enabled_accounts', 0)} 个启用账号）："
            "同步收信、列出和查看邮件、按会话查看"
        )
        lines.append("· 回复邮件：只为已收到的邮件起草回复（不能新建任意收件人的邮件）")
    else:
        lines.append("· 邮件：当前没有配置任何邮箱")
    if state_of("mail_send_after_confirmation") == "available":
        lines.append(
            "· 发送邮件：先准备，再把将要发出的完整内容给你看；"
            "只有你明确回复「确认发送」才会发出"
        )
    else:
        lines.append("· 发送邮件：需要先配置发信（SMTP）账号")
    lines.append("")
    if recurring.get("weekly_only"):
        lines.append("做不到的：单双周、每两周一次、每月或每年重复、节假日或考试周除外。")
    lines.append("做不到的：新建一封任意收件人的邮件、提交校外系统的手续（eHall）、")
    lines.append("创建审批或绕过确认执行外部动作。")
    return "\n".join(lines)


def _render_mail_status(data: dict[str, Any]) -> str:
    lines = [
        f"本地已收 {data.get('stored_messages', 0)} 封邮件。",
        f"其中 {data.get('requires_reply', 0)} 封被判定为需要回复。",
    ]
    waiting = int(data.get("waiting_sends", 0))
    lines.append("没有等待发送的邮件。" if not waiting else f"有 {waiting} 封已准备、等待确认。")
    unresolved = int(data.get("unresolved_sends", 0))
    if unresolved:
        lines.append(f"还有 {unresolved} 封的发送结果不确定，需要人工对账。")
    return "\n".join(lines)


def _render_mail_synced(data: dict[str, Any]) -> str:
    accounts = data.get("accounts") or []
    if not accounts:
        return "没有启用的邮件账号，所以没有收信。"
    lines = ["收信结果："]
    for account in accounts:
        lines.append(
            f"- {account.get('account_id', '')}：{account.get('status', '')}"
            f"（新 {account.get('new_messages', 0)} 封，"
            f"已存在 {account.get('matched_existing', 0)} 封）"
        )
    return "\n".join(lines)


def _render_mail_messages(data: dict[str, Any], timezone: str | None) -> str:
    messages = data.get("messages") or []
    if not messages:
        if data.get("requires_reply_filter"):
            return "没有需要回复的邮件。"
        return "本地还没有收到邮件。"
    lines = [f"最近 {len(messages)} 封："]
    for message in messages:
        lines.append(f"- {_mail_line(message, timezone)}")
    lines.append("要回复哪一封，直接说发件人或主题就行。")
    return "\n".join(lines)


def _render_mail_message(data: dict[str, Any], timezone: str | None) -> str:
    header = [
        f"发件人：{data.get('from_address') or '（未知）'}",
        f"主题：{data.get('subject') or '（无主题）'}",
        f"时间：{_moment(data.get('received_at'), timezone)}",
    ]
    if data.get("category"):
        header.append(f"分类：{data['category']}")
    if data.get("requires_reply"):
        header.append("分析结论：需要回复")
    body = data.get("body") or ""
    if not data.get("body_available"):
        return "\n".join([*header, "", "这封邮件只存了头部，正文没有可取出的内容。"])
    suffix = "……（正文较长，已截断）" if data.get("body_truncated") else ""
    return "\n".join([*header, "", body + suffix])


def _render_mail_thread(data: dict[str, Any], timezone: str | None) -> str:
    messages = data.get("messages") or []
    if not messages:
        return "这个会话里没有已存储的邮件。"
    lines = [f"会话 {_short(data.get('thread_id'))}，共 {len(messages)} 封："]
    for message in messages:
        lines.append(f"- {_mail_line(message, timezone)}")
    return "\n".join(lines)


def _render_mail_draft(data: dict[str, Any]) -> str:
    recipients = "、".join(str(item) for item in data.get("to_addresses") or [])
    lines = [
        f"草稿已准备好（{_short(data.get('draft_id'))}，第 {data.get('version', 1)} 版）。",
        f"· 收件人：{recipients or '（无收件人）'}",
        f"· 主题：{data.get('subject', '')}",
        "",
        "正文：",
        "---",
        str(data.get("body") or ""),
        "---",
    ]
    questions = data.get("needs_user_input") or []
    if questions:
        lines.append("这封草稿里有需要你确认的地方：")
        lines += [f"- {question}" for question in questions]
        lines.append("确认或修改之后再说「准备发送」，我会把最终内容给你看。")
    else:
        lines.append("要发送的话说「准备发送」，我会把将要发出的内容完整给你看。")
    return "\n".join(lines)


def _render_mail_reconciled(data: dict[str, Any]) -> str:
    result = data.get("result")
    if data.get("already_sent"):
        return "这封邮件已经在发件箱里，确认已经送达。"
    if result == "found":
        return "在发件箱里找到了这封邮件，确认已经发送。"
    if result == "not_found":
        return (
            "发件箱里没有找到它。这**不能**证明它没有发出去——"
            "有些服务器不会把已发送的邮件放在可搜索的位置。我不会自动重发。"
        )
    if result == "unavailable":
        return "现在没法查发件箱（没有可用的收信凭证或连接失败），发送状态仍然不确定。"
    return f"对账结果：{result}。"


def _mail_line(message: dict[str, Any], timezone: str | None) -> str:
    parts = [
        f"[{_short(message.get('message_id'))}]",
        str(message.get("from_address") or "（未知发件人）"),
        str(message.get("subject") or "（无主题）"),
        _moment(message.get("received_at"), timezone),
    ]
    if message.get("requires_reply"):
        parts.append("需要回复")
    return " · ".join(parts)


def _text_field(payload: dict[str, object], name: str) -> str:
    value = payload.get(name)
    return "" if value is None else str(value)


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


def render_preflight_refused(reason: str) -> str:
    """A plan could not be carried out in full, so none of it was applied (ADR-0035 §15-§16)."""
    return f"我没有执行任何操作：{reason}。你可以调整一下再说一次。"


def render_mail_send_preview(payload: dict[str, object]) -> str:
    """The exact bytes that would leave the machine, from the immutable action payload.

    This is derived from the stored `ActionRequest` payload and from nothing else — not from the
    draft, not from the model's summary. What the user reads here is what the executor sends.
    """
    account = _text_field(payload, "account_id")
    sender = _text_field(payload, "from_address")
    raw_recipients = payload.get("to_addresses")
    recipients = raw_recipients if isinstance(raw_recipients, list) else []
    to_line = "、".join(str(item) for item in recipients) if recipients else "（无收件人）"
    subject = _text_field(payload, "subject")
    body = str(payload.get("body_text") or "")
    lines = [
        "将要发送的邮件（以下内容就是实际发出的内容）：",
        f"· 发件账号：{account}（{sender}）" if sender else f"· 发件账号：{account}",
        f"· 收件人：{to_line}",
        f"· 主题：{subject}",
    ]
    reply_to = payload.get("in_reply_to_header")
    if reply_to:
        lines.append(f"· 回复对象：{reply_to}")
    message_id = payload.get("rfc_message_id")
    if message_id:
        lines.append(f"· Message-ID：{message_id}")
    version = payload.get("draft_version")
    if version is not None:
        lines.append(f"· 草稿版本：{version}")
    lines += [
        "",
        "正文：",
        "---",
        body,
        "---",
        "",
        "确认发送吗？回复「确认发送」我就发送，或回复「取消」。",
        "如果内容还要改，直接说出改动，我会重新准备一份再给你确认。",
    ]
    return "\n".join(lines)


def render_send_result(status: str | None, failure_reason: str | None = None) -> str:
    """What happened to one reviewed send, in the words the user needs."""
    if status == "succeeded":
        return "已发送。"
    if status == "unknown":
        return (
            "发送结果不确定：连接在邮件数据被接受之后断开了，我无法本地判断它是否送达。\n"
            "我不会自动重试，以免重复发送。你可以说「帮我查一下有没有发出去」。"
        )
    detail = "" if not failure_reason else f"（{failure_reason}）"
    return f"发送失败，没有确认产生外部发送结果。{detail}"


_INTERNAL_MARKERS = (
    "traceback",
    "jsonschema",
    "validationerror",
    "required property",
    "is not of type",
    "is not valid under any",
    "sqlite3.",
    "keyerror",
    "assertionerror",
    "model output violates",
)
"""Substrings that must never reach the normal user interface (ADR-0035 §8, §32)."""

_ERROR_SENTENCES: dict[ConversationErrorCode, str] = {
    ConversationErrorCode.INPUT_DECODE_FAILED: (
        "刚才的输入包含无法识别的终端字符，我没有执行任何操作。请重新输入这一句。"
    ),
    ConversationErrorCode.MODEL_TIMEOUT: (
        "模型这次没有及时回应，所以什么都没有执行。要不要再说一次？"
    ),
    ConversationErrorCode.MODEL_INVALID_OUTPUT: (
        "我刚才没能可靠地理解这句话，因此没有执行任何操作。你可以换一种说法再试一次。"
    ),
    ConversationErrorCode.MODEL_REPAIR_FAILED: (
        "我刚才没能可靠地理解这句话，因此没有执行任何操作。你可以换一种说法再试一次。"
    ),
    ConversationErrorCode.INTERNAL_ERROR: (
        "刚才那一步出了内部错误，我没有继续执行。可以再说一次，或者用 `pw` 检查当前状态。"
    ),
}


def render_error(
    code: ConversationErrorCode,
    detail: str | None = None,
    *,
    debug: bool = False,
) -> str:
    """One recoverable failure, in words; internals only when debug is on.

    A failure the user can act on gets a sentence with no jargon. `detail` is kept for the debug
    path and for tests: it is attached only when debug is on, and never when it looks like an
    internal error string.
    """
    sentence = _ERROR_SENTENCES.get(
        code, _ERROR_SENTENCES[ConversationErrorCode.INTERNAL_ERROR]
    )
    if not debug or not detail:
        return sentence
    safe = sanitise_detail(detail)
    return sentence if safe is None else f"{sentence}\n[debug] {safe}"


def sanitise_detail(detail: str) -> str | None:
    """Return `detail` only when it carries no internal machinery."""
    lowered = detail.lower()
    if any(marker in lowered for marker in _INTERNAL_MARKERS):
        return None
    trimmed = detail.strip()
    return trimmed[:300] or None


def render_review_withdrawn() -> str:
    """The user withdrew the pending send."""
    return "好，这封邮件没有发送。需要的时候再说一次，我会重新准备。"


def render_review_expired() -> str:
    """The reviewed send waited too long."""
    return "这次确认已经过期了，我没有发送。重新说一下要回什么，我会再准备一份给你确认。"


def render_review_stale(reason: str | None = None) -> str:
    """The reviewed action no longer matches what was shown."""
    detail = "" if not reason else f"（{reason}）"
    return f"这封邮件的内容已经变了，之前那份确认作废{detail}。请让我重新准备一份再确认。"


def render_review_ambiguous(count: int) -> str:
    """More than one send is waiting; the runtime refuses to guess."""
    return (
        f"现在有 {count} 封邮件在等待确认，我不能从一句话里决定发哪一封。"
        "请先让我把要发的那一封重新给你看一遍，再确认发送。"
    )


def render_review_pending_notice() -> str:
    """A restart found a reviewed send still waiting for the human."""
    return "上次有一封尚未发送的邮件在等待确认，我把它的内容重新给你看一遍。"


def render_confirmation_request(operation_type: str) -> str:
    """Ask for the one confirmation a `CONFIRM_LOCAL` operation needs."""
    return (
        f"这一步需要你确认：{operation_type}。"
        "回复「可以」我就执行，或回复「取消」。"
    )


def render_recurring_confirmation_request(rules: Sequence[dict[str, Any]]) -> str:
    """Ask before a weekly commitment becomes durable state (ADR-0036 §19).

    Saying "我每周一十点到十二点有课" states a fact, not an instruction. The runtime asks rather
    than deciding for the user, and nothing is written until the answer arrives.
    """
    joined = "\n".join(f"- {_recurring_line(rule)}" for rule in rules)
    return (
        "要把这些加入固定安排吗？\n"
        f"{joined}\n"
        "回复「可以」我就保存，或回复「取消」。"
    )


def render_unsupported_recurrence(reason: str) -> str:
    """A recurrence this build cannot express, refused instead of approximated (ADR-0036 §12)."""
    return (
        f"「{reason}」这类重复规则我还不能记。"
        "现在只支持每周固定星期几，比如「每周一 10:00–12:00」。"
        "需要的话，我可以按每周同一时间来记。"
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
    recurring = data.get("recurring") or []
    blocks = data.get("plan_blocks") or []
    if not events and not recurring and not blocks:
        return f"接下来 {data.get('window_days', 0)} 天没有已记录的日程或计划。"
    lines = [f"接下来 {data.get('window_days', 0)} 天："]
    for event in events:
        lines.append(
            f"- 日程「{event.get('title', '')}」"
            f"（{_moment(event.get('starts_at'), timezone)} → "
            f"{_moment(event.get('ends_at'), timezone)}）"
        )
    for occurrence in recurring:
        lines.append(f"- {_recurring_occurrence_line(occurrence)}")
    for block in blocks:
        label = block.get("title", "")
        lines.append(
            f"- 计划块「{label}」"
            f"（{_moment(block.get('starts_at'), timezone)} → "
            f"{_moment(block.get('ends_at'), timezone)}）"
        )
    return "\n".join(lines)


def _recurring_occurrence_line(occurrence: dict[str, Any]) -> str:
    """One derived weekly class the way a person reads a week: `周一 10:00-12:00 课程 (每周)`.

    The occurrence is shown in its own rule's timezone, because that is the zone its civil time
    means something in; nothing derived (no id, no fingerprint) appears here.
    """
    zone = _zone(occurrence.get("timezone"))
    start = _parsed(occurrence.get("starts_at"))
    end = _parsed(occurrence.get("ends_at"))
    if start is None or end is None:  # pragma: no cover - the handler always sends ISO instants
        return str(occurrence.get("title", ""))
    local_start = start.astimezone(zone)
    local_end = end.astimezone(zone)
    return (
        f"{weekday_label(local_start.isoweekday())} "
        f"{local_start.strftime('%H:%M')}–{local_end.strftime('%H:%M')} "
        f"{occurrence.get('title', '')}（每周）"
    )


def _render_recurring_rules(data: dict[str, Any]) -> str:
    rules = data.get("rules") or []
    if not rules:
        return "现在还没有固定的每周安排。"
    lines = [f"现在有 {len(rules)} 条固定安排："]
    for rule in rules:
        suffix = "" if not rule.get("ends_on") else f"（到 {rule['ends_on']} 止）"
        state = "" if rule.get("status", "active") == "active" else f"（{rule.get('status')}）"
        lines.append(f"- {_recurring_line(rule)}{suffix}{state}")
    return "\n".join(lines)


def _render_recurring_created(data: dict[str, Any]) -> str:
    line = _recurring_line(data)
    if not data.get("created", True):
        return f"这个固定安排已经存在，我没有重复添加：{line}。"
    return f"已加入固定安排：\n{line}\n{_recurring_window_sentence(data)}"


def _render_recurring_updated(data: dict[str, Any]) -> str:
    return f"已更新固定安排：\n{_recurring_line(data)}\n{_recurring_window_sentence(data)}"


def _render_recurring_retired(data: dict[str, Any]) -> str:
    weekday = weekday_label(_weekday_of(data))
    return (
        f"以后{weekday}不再有「{data.get('title', '')}」了。"
        f"它原来的时间是 {_weekly_phrase(data)}；已经过去的工作记录和计划都没有改动。"
    )


def _recurring_line(rule: dict[str, Any]) -> str:
    """One rule in one line: its weekday, its local hours and its title."""
    return f"{_weekly_phrase(rule)} · {rule.get('title', '')}"


def _weekly_phrase(rule: dict[str, Any]) -> str:
    return (
        f"每{weekday_label(_weekday_of(rule))} "
        f"{rule.get('start_local_time', '')}–{rule.get('end_local_time', '')}"
    )


def _weekday_of(rule: dict[str, Any]) -> int:
    value = rule.get("weekday")
    return value if isinstance(value, int) else 0


def _recurring_window_sentence(rule: dict[str, Any]) -> str:
    starts_on = rule.get("starts_on")
    ends_on = rule.get("ends_on")
    if ends_on:
        return f"从 {starts_on} 起，到 {ends_on} 止。"
    return f"从 {starts_on} 起持续到你删除。"


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


def _parsed(value: object) -> datetime | None:
    """An ISO instant from a result payload, or `None` when there is not one."""
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:  # pragma: no cover - every caller passes an ISO string it produced
        return None


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
    "render_capabilities",
    "render_confirmation_expired",
    "render_confirmation_rejected",
    "render_confirmation_request",
    "render_empty_turn",
    "render_error",
    "render_failure",
    "render_interpretation_refused",
    "render_mail_send_preview",
    "render_multiple_pending",
    "render_preflight_refused",
    "render_recurring_confirmation_request",
    "render_result",
    "render_results",
    "render_review_ambiguous",
    "render_review_expired",
    "render_review_pending_notice",
    "render_review_stale",
    "render_review_withdrawn",
    "render_send_result",
    "render_unknown_local",
    "render_unsupported",
    "render_unsupported_recurrence",
]
