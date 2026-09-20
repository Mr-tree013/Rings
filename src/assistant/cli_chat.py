"""`rings` and `pw chat`: the Tree conversation, in a terminal (ADR-0033 §17-18).

Both entry points run the same `ConversationService` built by the same composition helper, so
there is no second conversational implementation to keep honest. The interactive shell itself is
deliberately thin: it reads a line, hands it to the runtime, prints what the runtime said, and
understands five session controls (`/help`, `/new`, `/threads`, `/use`, `/exit`) that are not
domain operations.

`/help` shows natural-language examples, not the 100+ `pw` commands: the point of this surface is
that the user speaks in goals.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import typer

from assistant import bootstrap
from assistant.application.conversation_service import ConversationService
from assistant.cli_support import console, fail
from assistant.domain.config import AssistantConfig
from assistant.domain.conversation import ConversationThread
from assistant.domain.errors import (
    DomainError,
    InvalidAssistantConfig,
    InvalidConversationThread,
    ModelCredentialsMissing,
    ModelNotConfigured,
)

GREETING = "你好，我是 Tree。今天想安排什么？"
RESUMED_NOTICE = "已继续上次对话。"
GOODBYE = "好，需要的时候再叫我。"

HELP_TEXT = """\
直接说你想做什么就行。例如：
  · 明天下午三点提醒我交软件工程报告
  · 我这周还有什么任务？
  · 帮我安排一下这周
  · 周四下午两点到四点有课
  · 今天论文写了一个半小时
  · 刚才那个任务我已经做完了
  · 这份资料里有没有提到申请截止日期？
  · 最近有什么需要处理的邮件？
  · 回复刚才那封，说我周五之前交。
  · 确认发送
  · 不要发
  · 帮我查一下刚才那封邮件有没有发出去。

会话控制（不是领域操作）：
  /help          显示这些例子
  /new           开始一个新会话
  /threads       列出最近的会话
  /use <id>      切换到某个会话
  /exit          退出

发邮件这件事：我会先起草，然后把**将要发出的完整内容**给你看，只有你回复「确认发送」才会发出。
「可以」「好」不会发送邮件。提交校外系统的手续（eHall）仍然不在这里。
"""

CONTROL_HELP = "只认识 /help、/new、/threads、/use、/exit 这几个会话控制。"


def run_conversation(
    *, input_fn: Callable[[str], str] = input, announce: str | None = None
) -> int:
    """Run one interactive Tree session. Returns the process exit code."""
    try:
        return asyncio.run(_main(input_fn=input_fn, announce=announce))
    except InvalidAssistantConfig as exc:
        fail(f"invalid configuration: {exc}", code=2)
    except ModelNotConfigured as exc:
        fail(
            f"{exc}\n提示：先在 config.toml 里配置 [model]，"
            f"并在环境里设置 {bootstrap.MODEL_API_KEY_ENV}。"
        )
    except ModelCredentialsMissing as exc:
        fail(f"{exc}\n提示：在环境里设置 {bootstrap.MODEL_API_KEY_ENV} 之后再试。")
    except DomainError as exc:
        fail(str(exc))
    return 1


async def _main(*, input_fn: Callable[[str], str], announce: str | None) -> int:
    """Load the host configuration, then run one conversation session."""
    config = await bootstrap.config_loader().load()
    return await _session(config, input_fn=input_fn, announce=announce)


async def _session(
    config: AssistantConfig,
    *,
    input_fn: Callable[[str], str],
    announce: str | None,
) -> int:
    """One event loop for the whole conversation: the adapter is built once, not per turn."""
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    adapter = bootstrap.model_adapter(config)
    try:
        service = bootstrap.conversation_service(database, clock, config, model=adapter)
        for notice in await service.recover_interrupted():
            console.print(f"[yellow]{notice}[/yellow]")
        thread, resumed = await service.resume_or_start()
        if announce:
            console.print(announce)
        console.print(f"[bold]Tree >[/bold] {GREETING}")
        if resumed:
            console.print(f"[dim]{RESUMED_NOTICE}[/dim]")
        pending = await service.pending_external_preview(thread.id)
        if pending is not None:
            # A reviewed send survives a restart; it is re-shown, never re-executed (§17).
            console.print(f"[bold]Tree >[/bold] {pending}")
        while True:
            try:
                line = input_fn("You > ")
            except (EOFError, KeyboardInterrupt):
                console.print()
                console.print(GOODBYE)
                return 0
            text = line.strip()
            if not text:
                continue
            if text.startswith("/"):
                switched = await _control(service, thread, text)
                if switched is None:
                    console.print(GOODBYE)
                    return 0
                thread = switched
                continue
            reply = await service.send(thread.id, text)
            console.print(f"[bold]Tree >[/bold] {reply.text}")
    finally:
        await bootstrap.close_model(adapter)


async def _control(
    service: ConversationService, thread: ConversationThread, text: str
) -> ConversationThread | None:
    """Handle one session control. `None` means the user asked to leave."""
    command, _, argument = text.partition(" ")
    command = command.lower()
    if command in ("/exit", "/quit"):
        return None
    if command == "/help":
        console.print(HELP_TEXT)
        return thread
    if command == "/new":
        await service.archive_thread(thread.id)
        console.print(f"[bold]Tree >[/bold] {GREETING}")
        return await service.start_thread()
    if command == "/threads":
        threads = await service.list_threads(limit=10)
        if not threads:
            console.print("还没有任何会话。")
            return thread
        for entry in threads:
            marker = "*" if entry.id == thread.id else " "
            console.print(
                f"{marker} [{str(entry.id)[:8]}] {entry.status.value} "
                f"{entry.title or '（未命名）'}"
            )
        return thread
    if command == "/use":
        if not argument.strip():
            console.print("用法：/use <会话 id 或前缀>")
            return thread
        try:
            switched = await service.reopen_thread(argument)
        except (DomainError, InvalidConversationThread) as exc:
            console.print(f"没有切换：{exc}")
            return thread
        console.print(f"[dim]已切换到 {str(switched.id)[:8]}。[/dim]")
        return switched
    console.print(CONTROL_HELP)
    return thread


def chat() -> None:
    """Talk to Tree in an interactive conversation (the same runtime as `rings`)."""
    raise typer.Exit(code=run_conversation())


def main() -> None:
    """The `rings` entry point: an interactive Tree conversation."""
    raise SystemExit(run_conversation(announce="Rings — 本地个人运营系统"))


def register(app: typer.Typer) -> None:
    """Register `pw chat` on the root app."""
    app.command("chat")(chat)


__all__ = [
    "CONTROL_HELP",
    "GOODBYE",
    "GREETING",
    "HELP_TEXT",
    "chat",
    "main",
    "register",
    "run_conversation",
]
