"""`rings` and `pw chat`: the Tree conversation, in a terminal (ADR-0033 §17-18, ADR-0035).

Both entry points run the same `ConversationService`, so there is one runtime to keep honest. What
this module adds is the *surrounding* reliability the product needs, and nothing else:

* input arrives through the terminal boundary: a real terminal gets a real line editor
  (`prompt_toolkit`), while piped or scripted input keeps `ConsoleInput` and strict decoding
  (ADR-0035 §4-§6, ADR-0040 §5). Either way a failure to read a line is a recoverable message
  instead of the end of the session, and Ctrl-C discards only the unfinished line;
* every turn runs inside an error boundary: a recoverable failure becomes a sentence, the loop
  continues, and internals go to stderr only when `RINGS_DEBUG=1` is set (§7-§9);
* `/help` is rendered from the same capability snapshot the runtime uses (§15), so it cannot drift
  away from what the build actually does.

The loop never logs message text, model text, mail content or challenge material: only ids, the
conversation error code and the exception class (§33).
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
import traceback
import webbrowser
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

import typer

from assistant import bootstrap, cli_up
from assistant.adapters.web.server import lan_addresses
from assistant.application import conversation_render as render
from assistant.application.conversation_capabilities.introspection import (
    build_capability_snapshot,
)
from assistant.application.conversation_input import (
    ConversationInputSource,
    build_input_source,
)
from assistant.application.conversation_service import ConversationService
from assistant.cli_support import console, error_console
from assistant.domain.config import AssistantConfig
from assistant.domain.conversation import ConversationThread
from assistant.domain.conversation_errors import ConversationErrorCode
from assistant.domain.errors import (
    DomainError,
    InvalidAssistantConfig,
    InvalidConversationThread,
    ModelCredentialsMissing,
    ModelNotConfigured,
)
from assistant.domain.mobile import MobileBindMode

LOGGER = logging.getLogger("assistant.conversation")

DEBUG_ENV = "RINGS_DEBUG"
"""Set it to `1` to see tracebacks and validation detail on stderr (ADR-0035 §9)."""

GREETING = "你好，我是 Tree。今天想安排什么？"
RESUMED_GREETING = "已继续上次对话。今天想处理什么？"
GOODBYE = "好，需要的时候再叫我。"

HELP_HEADER = """\
直接说你想做什么就行。例如：
  · 明天下午三点提醒我交软件工程报告
  · 我这周还有什么任务？
  · 帮我安排一下这周
  · 周四下午两点到四点有课
  · 每周一十点到十二点有课，记下来，再帮我安排下周
  · 我有哪些固定安排？
  · 今天论文写了一个半小时
  · 刚才那个任务我已经做完了
  · 这份资料里有没有提到申请截止日期？
  · 最近有什么需要处理的邮件？
  · 回复刚才那封，说我周五之前交。
  · 发个打招呼的邮件给我自己。
  · 张老师邮箱是 zhang@example.edu，记成联系人。
  · 给张老师发邮件，说我周五之前交报告。
  · 我有哪些联系人？
  · 记住我的办公室在仙林。
  · 确认记住
  · 你记得我的办公室在哪里吗？
  · 我今天有什么事？
  · 确认发送
  · 不要发
  · 帮我查一下刚才那封邮件有没有发出去。

会话控制（不是领域操作）：
  /help          显示这些例子和当前能力
  /new           开始一个新会话
  /threads       列出最近的会话
  /use <id>      切换到某个会话
  /exit          退出
"""

HELP_TAIL = """\
发邮件这件事：我会先起草，然后把将要发出的完整内容给你看，只有你回复「确认发送」才会发出。
「可以」「好」不会发送邮件。
"""

CONTROL_HELP = "只认识 /help、/new、/threads、/use、/exit 这几个会话控制。"

_SLOW_OPERATION_HINTS = {
    "mail.sync": "正在查看邮箱……",
    "knowledge.ask": "正在查资料……",
}


def debug_enabled() -> bool:
    """Whether this process was started with developer diagnostics on."""
    return os.environ.get(DEBUG_ENV, "").strip().lower() not in ("", "0", "false", "no")


def help_text(config: AssistantConfig | None) -> str:
    """The conversation's help, with capabilities read from the runtime (ADR-0035 §15)."""
    snapshot = build_capability_snapshot(config)
    return "\n".join(
        [HELP_HEADER, render.render_capabilities({"areas": snapshot.to_payload()}), HELP_TAIL]
    )


def run_conversation(
    *, input_fn: Callable[[str], str] | None = None, announce: str | None = None
) -> int:
    """Run one interactive Tree session. Returns the process exit code.

    It never raises for a recoverable condition: a missing credential, an unreadable configuration
    or an unexpected failure all end as a printed sentence and an exit code.
    """
    debug = debug_enabled()
    try:
        return asyncio.run(_main(input_fn=input_fn, announce=announce, debug=debug))
    except KeyboardInterrupt:
        # A Ctrl-C that arrives outside the line editor (mid-answer, say) still leaves quietly:
        # the editor's own Ctrl-C is handled as a cancelled line and never reaches this point.
        console.print()
        console.print(GOODBYE)
        return 130
    except InvalidAssistantConfig as exc:
        return _report(f"invalid configuration: {exc}", code=2, debug=debug, detail=str(exc))
    except ModelNotConfigured as exc:
        return _report(
            f"{exc}\n提示：先在 config.toml 里配置 [model]，并在环境里设置 "
            f"{bootstrap.MODEL_API_KEY_ENV}。",
            code=1,
            debug=debug,
            detail=str(exc),
        )
    except ModelCredentialsMissing as exc:
        return _report(
            f"{exc}\n提示：在环境里设置 {bootstrap.MODEL_API_KEY_ENV} 之后再试。",
            code=1,
            debug=debug,
            detail=str(exc),
        )
    except DomainError as exc:
        return _report(str(exc), code=1, debug=debug, detail=str(exc))
    except Exception as exc:
        return _report(
            render.render_error(ConversationErrorCode.INTERNAL_ERROR),
            code=1,
            debug=debug,
            detail=f"{type(exc).__name__}: {exc}",
        )


async def _main(
    *, input_fn: Callable[[str], str] | None, announce: str | None, debug: bool
) -> int:
    config = await bootstrap.config_loader().load()
    return await _session(config, input_fn=input_fn, announce=announce, debug=debug)


async def _session(
    config: AssistantConfig,
    *,
    input_fn: Callable[[str], str] | None,
    announce: str | None,
    debug: bool,
) -> int:
    """One event loop for the whole conversation: the adapter is built once, not per turn."""
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    adapter = bootstrap.model_adapter(config)
    reader = None if input_fn is not None else build_input_source()
    try:
        service = bootstrap.conversation_service(database, clock, config, model=adapter)
        for notice in await service.recover_interrupted():
            console.print(f"[yellow]{notice}[/yellow]")
        thread, resumed = await service.resume_or_start()
        if announce:
            console.print(announce)
        console.print(f"[bold]Tree >[/bold] {RESUMED_GREETING if resumed else GREETING}")
        pending = await service.pending_external_preview(thread.id)
        if pending is not None:
            # A reviewed send survives a restart; it is re-shown, never re-executed (ADR-0034 §17).
            console.print(f"[bold]Tree >[/bold] {pending}")
        waiting_fact = await service.pending_fact_review()
        if waiting_fact is not None:
            # A fact proposal survives a restart for the same reason: the row *is* the review, and
            # showing it again is not confirming it (ADR-0038 §13).
            console.print(f"[bold]Tree >[/bold] {waiting_fact}")
        while True:
            text, should_exit = await _read(reader, input_fn, debug)
            if should_exit:
                console.print(GOODBYE)
                return 0
            if text is None:
                continue
            if not text.strip():
                continue
            if text.startswith("/"):
                switched = await _control(service, config, thread, text)
                if switched is None:
                    console.print(GOODBYE)
                    return 0
                thread = switched
                continue
            thread = await _turn(service, thread, text, debug=debug)
    finally:
        await bootstrap.close_model(adapter)


async def _read(
    reader: ConversationInputSource | None,
    input_fn: Callable[[str], str] | None,
    debug: bool,
) -> tuple[str | None, bool]:
    """Read one line. Returns `(text_or_None, should_exit)`.

    `text is None` means "nothing usable, keep prompting": either a decoding failure or an empty
    line. Neither is allowed to end the session, and neither may reach the model.

    The editor runs in a worker thread: `prompt_toolkit` drives its own event loop, and asyncio
    does not nest, so awaiting it here is what lets a real line editor live inside this runtime
    without blocking the loop that owns the conversation.
    """
    if reader is not None:
        user = await asyncio.to_thread(reader.read_line, "You > ")
        if user.is_closed:
            return None, True
        if user.is_cancelled:
            # Ctrl-C ends the line, not the session: no turn, no model call, no mutation.
            console.print()
            return None, False
        if user.is_decode_failure:
            answer = render.render_error(
                ConversationErrorCode.INPUT_DECODE_FAILED, user.detail, debug=debug
            )
            console.print(f"[bold]Tree >[/bold] {answer}")
            LOGGER.info("conversation input rejected code=input_decode_failed")
            return None, False
        return user.text, False
    try:
        assert input_fn is not None
        return input_fn("You > "), False
    except (EOFError, KeyboardInterrupt):
        console.print()
        return None, True
    except UnicodeDecodeError as exc:
        # A scripted or wrapped stdin can still hand over undecodable bytes.
        answer = render.render_error(
            ConversationErrorCode.INPUT_DECODE_FAILED, str(exc), debug=debug
        )
        console.print(f"[bold]Tree >[/bold] {answer}")
        LOGGER.info("conversation input rejected code=input_decode_failed")
        return None, False


async def _turn(
    service: ConversationService, thread: ConversationThread, text: str, *, debug: bool
) -> ConversationThread:
    """One user turn, inside the conversation's error boundary (ADR-0035 §7)."""
    try:
        reply = await service.send(
            thread.id, text, activity=lambda hint: console.print(f"[dim]{hint}[/dim]")
        )
    except (EOFError, KeyboardInterrupt):  # pragma: no cover - the reader handles these
        raise
    except Exception as exc:
        answer = render.render_error(
            ConversationErrorCode.INTERNAL_ERROR, type(exc).__name__, debug=debug
        )
        console.print(f"[bold]Tree >[/bold] {answer}")
        LOGGER.warning(
            "conversation turn failed thread=%s error=%s", thread.id, type(exc).__name__
        )
        _trace(debug, exc)
        return thread
    console.print(f"[bold]Tree >[/bold] {reply.text}")
    if reply.error_code is not None:
        LOGGER.info(
            "conversation turn failed thread=%s turn=%s code=%s",
            reply.thread_id,
            reply.turn_id,
            reply.error_code.value,
        )
    if debug and reply.debug_detail:
        error_console.print(f"[dim]{reply.debug_detail}[/dim]")
    return thread


async def _control(
    service: ConversationService,
    config: AssistantConfig,
    thread: ConversationThread,
    text: str,
) -> ConversationThread | None:
    """Handle one session control. `None` means the user asked to leave."""
    command, _, argument = text.partition(" ")
    command = command.lower()
    if command in ("/exit", "/quit"):
        return None
    if command == "/help":
        console.print(help_text(config))
        return thread
    if command == "/new":
        await service.archive_thread(thread.id)
        console.print(f"[bold]Tree >[/bold] {GREETING}")
        started, _ = await service.resume_or_start()
        return started
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


def _trace(debug: bool, exc: BaseException) -> None:
    if debug:
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)


def _report(message: str, *, code: int, debug: bool, detail: str | None = None) -> int:
    error_console.print(message)
    if debug and detail:
        error_console.print(f"[dim]{detail}[/dim]")
    return code


def chat() -> None:
    """Talk to Tree in an interactive conversation (the same runtime as `rings`)."""
    raise typer.Exit(code=run_conversation())


def main() -> None:
    """The `rings` entry point: an interactive Tree conversation, or `--web` to open the chat UI.

    The default is deliberately unchanged. `rings` is still the terminal conversation, and
    `rings --web` is a *convenience* that opens the browser surface this host already serves — it
    never starts a daemon, never runs a command and never automates a browser (ADR-0041 §54).
    `rings up` / `rings down` are the one-command start and stop path (ADR-0046).
    """
    arguments = sys.argv[1:]
    if not arguments:
        cli_up.load_secrets_for_entry_point()
        raise SystemExit(run_conversation(announce="Rings — 本地个人运营系统"))
    if arguments == [WEB_FLAG]:
        cli_up.load_secrets_for_entry_point()
        raise SystemExit(open_web_chat())
    if arguments[0] in {"up", "down"}:
        raise SystemExit(cli_up.main(arguments))
    error_console.print(f"无法识别的参数：{' '.join(arguments)}")
    error_console.print(
        "用法：rings [--web] | rings up [--foreground] [--no-open] | rings down"
    )
    raise SystemExit(2)


WEB_FLAG = "--web"
"""The only argument `rings` understands, and the only one it needs."""

WEB_PROBE_TIMEOUT_SECONDS = 0.5
"""How long `rings --web` waits to find out whether the control plane is actually running."""


def web_chat_url(config: AssistantConfig | None) -> str | None:
    """The `/chat` URL this host would serve, or `None` when it does not serve one.

    The address comes from `[mobile]` and from the kernel's own idea of this machine's LAN
    addresses — never from a user-supplied hostname, and never from a proxy header.
    """
    if config is None or config.mobile is None or not config.mobile.enabled:
        return None
    mode = MobileBindMode(config.mobile.bind)
    if mode is MobileBindMode.LOOPBACK:
        host = "127.0.0.1"
    else:
        addresses = lan_addresses()
        host = addresses[0] if addresses else "127.0.0.1"
    return f"http://{host}:{config.mobile.port}/chat"


def reachable(url: str, *, timeout: float = WEB_PROBE_TIMEOUT_SECONDS) -> bool:
    """Whether something is listening on that URL's host and port.

    A diagnostic, not a health check: it opens one TCP connection and closes it. Anything that
    fails — refused, timed out, no such host — is simply "not running", because that is what the
    user needs to be told.
    """
    parsed = urlsplit(url)
    if parsed.hostname is None or parsed.port is None:  # pragma: no cover - built by this module
        return False
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=timeout):
            return True
    except OSError:
        return False


def open_web_chat(
    *,
    config_path: Path | None = None,
    opener: Callable[[str], object] | None = None,
    probe: Callable[[str], bool] | None = None,
) -> int:
    """Open the Tree chat surface in this machine's browser, or explain why it cannot be opened.

    Returns the process exit code: 0 when a browser was asked to open the URL, 1 when the host has
    no chat surface to open. Nothing here starts a server; if the control plane is not running, the
    honest answer is that it is not running.
    """
    browser_launcher = webbrowser.open if opener is None else opener
    is_reachable = reachable if probe is None else probe
    try:
        loaded = asyncio.run(bootstrap.config_loader(config_path).load())
    except (DomainError, InvalidAssistantConfig) as exc:
        console.print(f"读不到主机配置，所以我没有打开网页界面：{exc}")
        console.print("先运行 `pw doctor` 看看配置哪里不对。")
        return 1
    url = web_chat_url(loaded)
    if url is None:
        # `markup=False`: these sentences name config keys, and a key is not a style tag.
        console.print(
            "这个主机还没有启用网页控制面（config 里的 [mobile] enabled）。", markup=False
        )
        console.print(
            "在 config.toml 里设置 [mobile] enabled = true 并重启 assistantd 之后再试一次。",
            markup=False,
        )
        return 1
    if not is_reachable(url):
        console.print(f"网页控制面没有在运行：{url}")
        console.print("先启动 `assistantd`，然后重新运行 `rings --web`。")
        console.print("终端对话仍然可以直接用：运行 `rings`。")
        return 1
    console.print(f"正在打开 Tree 网页对话：{url}")
    browser_launcher(url)
    return 0


def register(app: typer.Typer) -> None:
    """Register `pw chat` on the root app."""
    app.command("chat")(chat)


__all__ = [
    "CONTROL_HELP",
    "DEBUG_ENV",
    "GOODBYE",
    "GREETING",
    "HELP_HEADER",
    "RESUMED_GREETING",
    "WEB_FLAG",
    "chat",
    "debug_enabled",
    "help_text",
    "main",
    "open_web_chat",
    "reachable",
    "register",
    "run_conversation",
    "web_chat_url",
]
