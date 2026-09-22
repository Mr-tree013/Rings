"""`rings up` / `rings down` / `rings autostart`: the one-command startup path (ADR-0046).

The file is deliberately two halves:

* **decision logic** — `run_up`, `run_down`, `run_autostart` — which takes an `UpDeps` of callables
  and is therefore testable without a process, a socket or a browser;
* **rendering** — `render_up` — a pure function over the report, because the sentences a person
  reads are part of the product and deserve their own tests.

`default_deps()` is the only place that touches the real world.
"""

from __future__ import annotations

import asyncio
import os
import time
import webbrowser
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from assistant import bootstrap
from assistant.adapters.config.secrets_env import (
    SecretEnvResult,
    SecretEnvStatus,
    load_secret_environment_into_process,
)
from assistant.adapters.ehall.session import session_state
from assistant.adapters.runtime.daemon_process import (
    READINESS_TIMEOUT_SECONDS,
    DaemonState,
    assistantd_argv,
    daemon_log_path,
    daemon_state,
    probe_http,
    spawn_daemon,
    stop_daemon,
)
from assistant.adapters.runtime.windows_autostart import (
    AutostartSpec,
    autostart_status,
    current_distro,
    current_user,
    install_autostart,
    remove_autostart,
    render_autostart_cmd,
    resolved_uv,
    windows_startup_directory,
)
from assistant.application.paths import AppPaths
from assistant.cli_support import console


@dataclass(frozen=True, slots=True)
class MailSummary:
    """What the status block may say about mail: counts and readiness, never content."""

    accounts: int
    ready: int
    stored: int
    account_ids: tuple[str, ...] = ()
    """The configured accounts' local ids — safe metadata, the same ids the settings page shows."""


@dataclass(frozen=True, slots=True)
class EHallSummary:
    """The eHall pipeline's own state, as far as a local check can tell."""

    enabled: bool
    profile_present: bool


@dataclass(frozen=True, slots=True)
class UpReport:
    """Everything `rings up` learned, in the shape the renderer wants."""

    daemon_action: str
    pid: int | None
    version: str | None
    web_url: str | None
    paired: bool
    secrets: SecretEnvResult
    mail: MailSummary
    ehall: EHallSummary
    note: str | None = None


@dataclass(frozen=True, slots=True)
class UpDeps:
    """Every side effect `rings up` performs, as a callable."""

    load_config: Callable[[], Awaitable[Any]]
    daemon_state: Callable[[Path], DaemonState]
    spawn_daemon: Callable[[Path], int]
    probe_http: Callable[[str], bool]
    mint_pairing_token: Callable[[], Awaitable[Any]]
    open_chat_page: Callable[[str], bool]
    load_secrets: Callable[[], SecretEnvResult]
    runtime_root: Callable[[], Path]
    web_url: Callable[[Any], str | None]
    mail_summary: Callable[[Any], Awaitable[MailSummary]]
    ehall_summary: Callable[[Any], EHallSummary]
    sleep: Callable[[float], None]
    monotonic: Callable[[], float]


def render_up(report: UpReport) -> str:
    """The status block: six honest lines, and never a secret or a pairing token."""
    if report.daemon_action == "started":
        daemon = f"已启动（pid {report.pid}，{report.version}）"
    elif report.daemon_action == "reused":
        daemon = f"已在运行（pid {report.pid}，{report.version}）"
    else:
        daemon = "前台运行中"
    lines = [
        f"daemon        {daemon}",
        f"web           {report.web_url or '未启用'}",
        f"配对          {'本次已自动配对' if report.paired else '未执行'}",
    ]
    if report.secrets.status is SecretEnvStatus.REFUSED:
        lines.append(f"凭据          {report.secrets.reason}")
    elif report.secrets.status is SecretEnvStatus.LOADED:
        named = "、".join(report.secrets.names[:4])
        more = "" if len(report.secrets.names) <= 4 else f" 等 {len(report.secrets.names)} 个"
        lines.append(
            f"凭据          从 {report.secrets.path.name} 读到 {named}{more}"
            f"（新载入 {len(report.secrets.applied)} 项）"
        )
    else:
        lines.append("凭据          未配置（可选：~/.config/growing-assistant/secrets.env）")
    if report.mail.accounts == 0:
        lines.append("邮件          没有配置账号")
    else:
        named_accounts = "、".join(report.mail.account_ids) or f"{report.mail.accounts} 个"
        lines.append(
            f"邮件          {named_accounts}：{report.mail.ready}/{report.mail.accounts} "
            f"个凭据就位，已存 {report.mail.stored} 封"
        )
    if not report.ehall.enabled:
        lines.append("eHall         未启用（[ehall] enabled = false）")
    elif report.ehall.profile_present:
        lines.append("eHall         profile 已存在；只有实时检查才知道登录是否仍然有效")
    else:
        lines.append("eHall         还没有登录，运行 `uv run pw ehall login` 自己登录一次")
    if report.web_url is None:
        lines.append("")
        lines.append(
            '要在浏览器里用，请在配置里启用 [mobile]（enabled = true, bind = "loopback"）。'
        )
        lines.append("终端对话可以直接用：`rings`。")
    if report.note:
        lines.append(report.note)
    return "\n".join(lines)


def run_up(
    *, deps: UpDeps | None = None, foreground: bool = False, open_chat_page: bool = True
) -> UpReport:
    """Start or reuse the daemon, pair this browser and open `/chat`.

    Raises:
        TimeoutError: the daemon never became ready; the message names its log file. The CLI turns
            that into a bounded failure report and a non-zero exit (spec D4) — it never claims a
            start that did not happen.
    """
    return asyncio.run(_run_up(deps=deps, foreground=foreground, open_chat_page=open_chat_page))


async def _run_up(*, deps: UpDeps | None, foreground: bool, open_chat_page: bool) -> UpReport:
    resolved = deps or default_deps()
    root = resolved.runtime_root()
    secrets = resolved.load_secrets()
    config = await resolved.load_config()
    state = resolved.daemon_state(root)
    if state.running:
        action = "reused"
    elif foreground:
        # This process is about to *become* the daemon (the caller execs it), so it must not also
        # start one, pair a browser or claim a pid it does not own.
        action = "foreground"
    else:
        resolved.spawn_daemon(root)
        state = _wait_for_daemon(resolved, root, url=resolved.web_url(config))
        action = "started"
    url = resolved.web_url(config)
    paired = False
    if url is not None and not foreground:
        issued = await resolved.mint_pairing_token()
        if open_chat_page:
            resolved.open_chat_page(f"{url}#pair={issued.token}")
        paired = True
    note = None
    if foreground and action == "foreground":
        note = "本次为前台运行，不自动配对；已配对过的浏览器可直接打开上面的地址。"
    return UpReport(
        daemon_action=action,
        pid=state.pid,
        version=state.version,
        web_url=url,
        paired=paired,
        secrets=secrets,
        mail=await resolved.mail_summary(config),
        ehall=resolved.ehall_summary(config),
        note=note,
    )


def _wait_for_daemon(deps: UpDeps, root: Path, *, url: str | None) -> DaemonState:
    """Wait for the lock (and, when there is one, the control plane) to be ready."""
    deadline = deps.monotonic() + READINESS_TIMEOUT_SECONDS
    while deps.monotonic() < deadline:
        state = deps.daemon_state(root)
        if state.running and (url is None or deps.probe_http(url)):
            return state
        deps.sleep(0.25)
    raise TimeoutError(f"assistantd 没有在限定时间内就绪；日志：{daemon_log_path(root)}")


def run_down(*, deps: UpDeps | None = None) -> int:
    """Stop the daemon, or say that there was nothing to stop."""
    resolved = deps or default_deps()
    outcome = stop_daemon(resolved.runtime_root())
    if outcome.pid is None:
        console.print("没有在运行。")
        return 0
    if outcome.stopped:
        console.print(
            f"已停止（pid {outcome.pid}）。进行中的对话会被标记为 interrupted，不会重放。"
        )
        return 0
    console.print(f"还没有停下来（pid {outcome.pid} 仍在运行），看一眼日志再试。")
    return 1


def run_autostart(action: str, *, repo: Path | None = None) -> int:
    """`install` / `status` / `remove` the one Startup file, printing exactly what it did."""
    startup = windows_startup_directory()
    if startup is None:
        console.print(
            "没有找到 Windows 启动文件夹（这个环境看起来不是 WSL，或者 %APPDATA% 读不到）。"
        )
        return 1
    if action == "status":
        status = autostart_status(startup_dir=startup)
        console.print(f"启动项：{status.path}")
        console.print("状态：已安装" if status.installed else "状态：未安装")
        if status.content:
            console.print(status.content.rstrip("\r\n"), markup=False)
        return 0
    if action == "remove":
        removed = remove_autostart(startup_dir=startup)
        console.print("已删除启动项。" if removed else "本来就没有安装启动项。")
        return 0
    if action != "install":
        console.print("用法：rings autostart install|status|remove [--repo PATH]", markup=False)
        return 2
    checkout = Path(repo) if repo is not None else Path(bootstrap.__file__).resolve().parents[1]
    if not (checkout / "pyproject.toml").is_file():
        console.print(f"{checkout} 看起来不是 Rings 仓库；用 --repo 指定仓库路径。")
        return 2
    distro = current_distro()
    if distro is None:
        console.print("读不到 WSL 发行版名（wsl.exe 不可用），无法生成启动项。")
        return 1
    spec = AutostartSpec(
        distro=distro,
        user=current_user(),
        repo=checkout,
        runtime_root=AppPaths.resolve().runtime,
        uv=resolved_uv(),
    )
    path = install_autostart(spec, startup_dir=startup)
    console.print(f"已写入 {path}：")
    console.print(render_autostart_cmd(spec).rstrip("\r\n"), markup=False)
    console.print("删掉这个文件（或 `rings autostart remove`）即可撤销。")
    return 0


def read_log_tail(runtime_root: Path, *, lines: int = 8) -> str:
    """The last few lines of the daemon log, so a bounded failure says something useful."""
    path = daemon_log_path(Path(runtime_root))
    if not path.is_file():
        return "（还没有日志文件）"
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:]) or "（日志是空的）"


def default_deps() -> UpDeps:
    """The real world: bootstrap services, the process adapter, the browser and the clock."""
    # Imported here, not at module scope: `cli_chat` imports this module to dispatch the verbs, so
    # a top-level import of `web_chat_url` would be a cycle.
    from assistant.adapters.mail.credentials import available_password
    from assistant.cli_chat import web_chat_url

    def runtime_root() -> Path:
        return AppPaths.resolve().runtime

    async def load_config() -> Any:
        return await bootstrap.config_loader().load()

    async def mint_pairing_token() -> Any:
        clock = bootstrap.system_clock()
        database = bootstrap.runtime_database(clock)
        config = await load_config()
        return await bootstrap.mobile_auth_service(
            config, clock, database
        ).create_pairing_token()

    async def mail_summary(config: Any) -> MailSummary:
        if config is None or not config.mail.accounts:
            return MailSummary(accounts=0, ready=0, stored=0)
        clock = bootstrap.system_clock()
        database = bootstrap.runtime_database(clock)
        stored = await bootstrap.mail_repository(database).count_messages()
        ready = sum(
            1
            for account in config.mail.accounts
            if account.enabled and available_password(account.id) is not None
        )
        return MailSummary(
            accounts=len(config.mail.accounts),
            ready=ready,
            stored=stored,
            account_ids=tuple(account.id for account in config.mail.accounts),
        )

    def ehall_summary(config: Any) -> EHallSummary:
        state = session_state()
        return EHallSummary(
            enabled=config is not None and config.ehall.enabled,
            profile_present=state.profile_exists,
        )

    return UpDeps(
        load_config=load_config,
        daemon_state=daemon_state,
        spawn_daemon=lambda root: spawn_daemon(root),
        probe_http=lambda url: probe_http(url),
        mint_pairing_token=mint_pairing_token,
        open_chat_page=webbrowser.open,
        load_secrets=load_secrets_for_entry_point,
        runtime_root=runtime_root,
        web_url=web_chat_url,
        mail_summary=mail_summary,
        ehall_summary=ehall_summary,
        sleep=time.sleep,
        monotonic=time.monotonic,
    )


def load_secrets_for_entry_point() -> SecretEnvResult:
    """Load the user's secrets file, with the provider key named by the composition root."""
    return load_secret_environment_into_process(model_key=bootstrap.MODEL_API_KEY_ENV)


def _exec_daemon(environ: Mapping[str, str] | None = None) -> None:  # pragma: no cover
    """Become the daemon in this terminal: `--foreground` hands the process over to it."""
    argv = assistantd_argv()
    console.print("前台运行中：Ctrl-C 停止。")
    os.execvpe(argv[0], argv, dict(os.environ if environ is None else environ))


def main(arguments: Sequence[str], *, deps: UpDeps | None = None) -> int:
    """Dispatch `rings up|down`, or explain the usage. `autostart` is wired in the next task."""
    verb, rest = arguments[0], list(arguments[1:])
    if verb == "up":
        foreground = "--foreground" in rest
        try:
            report = run_up(
                deps=deps,
                foreground=foreground,
                open_chat_page=not foreground and "--no-open" not in rest,
            )
        except TimeoutError as exc:
            # Spec D4: a start that did not finish is reported, never claimed.
            console.print(f"Rings 没有在限定时间内就绪：{exc}")
            console.print("日志的最后几行：")
            console.print(read_log_tail((deps or default_deps()).runtime_root()))
            return 1
        # `markup=False`: the block names config keys (`[mobile]`, `[ehall]`), and a key is not a
        # rich style tag — without this the brackets are swallowed and the hint reads as noise.
        console.print(render_up(report), markup=False)
        if foreground and report.daemon_action == "foreground":
            _exec_daemon()
        return 0
    if verb == "down":
        return run_down(deps=deps)
    if verb == "autostart":
        action = ""
        repo: Path | None = None
        pending = list(rest)
        while pending:
            item = pending.pop(0)
            if item == "--repo" and pending:
                repo = Path(pending.pop(0))
            elif not action:
                action = item
        return run_autostart(action, repo=repo)
    console.print(
        "用法：rings up [--foreground] [--no-open] | rings down | "
        "rings autostart install|status|remove [--repo PATH]"
    )
    return 2


__all__ = [
    "EHallSummary",
    "MailSummary",
    "UpDeps",
    "UpReport",
    "default_deps",
    "main",
    "read_log_tail",
    "render_up",
    "run_autostart",
    "run_down",
    "run_up",
]
