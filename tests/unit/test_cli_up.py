"""`rings up` / `rings down` decision logic, with every side effect injected (ADR-0046)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from assistant.adapters.config.secrets_env import SecretEnvResult, SecretEnvStatus
from assistant.adapters.runtime.daemon_process import DaemonState
from assistant.cli_up import (
    EHallSummary,
    MailSummary,
    UpDeps,
    UpReport,
    main,
    render_up,
    run_down,
    run_up,
)


class _Fake:
    """One scripted dependency set: no process, no socket, no browser."""

    def __init__(
        self,
        *,
        running: bool = False,
        web: str | None = "http://127.0.0.1:8765/chat",
        ready: bool = True,
    ) -> None:
        self.running = running
        self.web = web
        self.ready = ready
        self.spawned = 0
        self.probes: list[str] = []
        self.opened: list[str] = []
        self.minted = 0
        self.now = 0.0
        self.stopped: list[Path] = []

    def deps(self, runtime_root: Path) -> UpDeps:
        def state(_root: Path) -> DaemonState:
            return DaemonState(
                running=self.running,
                pid=4242 if self.running else None,
                version="1.4.0" if self.running else None,
            )

        def spawn(_root: Path) -> int:
            self.spawned += 1
            self.running = True
            return 4242

        def probe(url: str) -> bool:
            self.probes.append(url)
            return self.ready

        async def mint() -> Any:
            self.minted += 1
            return type("Issued", (), {"token": "token-value"})()

        async def load_config() -> Any:
            return None

        def secrets() -> SecretEnvResult:
            return SecretEnvResult(
                path=runtime_root / "secrets.env", status=SecretEnvStatus.MISSING
            )

        def monotonic() -> float:
            self.now += 1.0
            return self.now

        def stop(root: Path) -> Any:
            self.stopped.append(root)
            was_running = self.running
            self.running = False
            from assistant.adapters.runtime.daemon_process import StopOutcome

            return StopOutcome(stopped=was_running, pid=4242 if was_running else None)

        import assistant.cli_up as cli_up_module

        original_stop = cli_up_module.stop_daemon
        cli_up_module.stop_daemon = stop  # type: ignore[assignment]

        def restore() -> None:
            cli_up_module.stop_daemon = original_stop  # type: ignore[assignment]

        self.restore = restore
        return UpDeps(
            load_config=load_config,
            daemon_state=state,
            spawn_daemon=spawn,
            probe_http=probe,
            mint_pairing_token=mint,
            open_chat_page=lambda url: (self.opened.append(url), True)[1],
            load_secrets=secrets,
            runtime_root=lambda: runtime_root,
            web_url=lambda _config: self.web,
            mail_summary=_mail,
            ehall_summary=_ehall,
            sleep=lambda _seconds: None,
            monotonic=monotonic,
        )


async def _mail(_config: object) -> MailSummary:
    return MailSummary(accounts=1, ready=1, stored=6)


def _ehall(_config: object) -> EHallSummary:
    return EHallSummary(enabled=True, profile_present=True)


def test_up_starts_pairs_and_opens(tmp_path: Path) -> None:
    fake = _Fake()

    report = run_up(deps=fake.deps(tmp_path))

    assert fake.spawned == 1
    assert fake.minted == 1
    assert fake.opened == ["http://127.0.0.1:8765/chat#pair=token-value"]
    assert report.daemon_action == "started"
    text = render_up(report)
    assert "已启动" in text
    assert "配对" in text
    assert "token-value" not in text  # the token never reaches the status block


def test_up_reuses_a_running_daemon_and_still_opens_the_chat(tmp_path: Path) -> None:
    fake = _Fake(running=True)

    report = run_up(deps=fake.deps(tmp_path))

    assert fake.spawned == 0
    assert report.daemon_action == "reused"
    assert fake.opened  # the browser is opened either way


def test_up_without_a_control_plane_says_so_and_opens_nothing(tmp_path: Path) -> None:
    fake = _Fake(web=None)

    report = run_up(deps=fake.deps(tmp_path))

    assert fake.opened == []
    assert fake.minted == 0
    assert report.web_url is None
    assert "[mobile]" in render_up(report)


def test_foreground_mode_neither_spawns_nor_pairs(tmp_path: Path) -> None:
    fake = _Fake()

    report = run_up(deps=fake.deps(tmp_path), foreground=True)

    assert fake.spawned == 0
    assert fake.minted == 0
    assert report.daemon_action == "foreground"
    assert "前台运行中" in render_up(report)


def test_a_daemon_that_never_becomes_ready_is_a_bounded_failure(
    tmp_path: Path, capsys: object
) -> None:
    fake = _Fake(ready=False)

    code = main(["up", "--no-open"], deps=fake.deps(tmp_path))

    assert code == 1
    assert fake.spawned == 1


def test_down_reports_when_nothing_was_running(tmp_path: Path) -> None:
    fake = _Fake()
    deps = fake.deps(tmp_path)

    try:
        assert run_down(deps=deps) == 0
    finally:
        fake.restore()


def test_the_status_block_never_prints_a_secret_value(tmp_path: Path) -> None:
    report = UpReport(
        daemon_action="started",
        pid=4242,
        version="1.4.0",
        web_url="http://127.0.0.1:8765/chat",
        paired=True,
        secrets=SecretEnvResult(
            path=tmp_path / "secrets.env",
            status=SecretEnvStatus.LOADED,
            names=("GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD",),
            applied=("GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD",),
        ),
        mail=MailSummary(accounts=1, ready=1, stored=6),
        ehall=EHallSummary(enabled=True, profile_present=False),
        note=None,
    )

    text = render_up(report)

    assert "GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD" in text  # the name is the useful part
    assert "pw ehall login" in text


def test_a_refused_secrets_file_is_reported_not_hidden(tmp_path: Path) -> None:
    report = UpReport(
        daemon_action="reused",
        pid=1,
        version="1.4.0",
        web_url=None,
        paired=False,
        secrets=SecretEnvResult(
            path=tmp_path / "secrets.env",
            status=SecretEnvStatus.REFUSED,
            reason="权限是 0644，必须是 0600（chmod 600 …）",
        ),
        mail=MailSummary(accounts=0, ready=0, stored=0),
        ehall=EHallSummary(enabled=False, profile_present=False),
        note=None,
    )

    text = render_up(report)

    assert "0600" in text


def test_the_status_block_reaches_the_terminal_with_its_brackets_intact(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """`[mobile]` is a config key, not a rich style tag: printing must not eat it."""
    import io

    from rich.console import Console

    import assistant.cli_up as cli_up_module

    buffer = io.StringIO()
    monkeypatch.setattr(cli_up_module, "console", Console(file=buffer, width=200, no_color=True))
    fake = _Fake(web=None)
    deps = fake.deps(tmp_path)
    try:
        code = cli_up_module.main(["up", "--no-open"], deps=deps)
    finally:
        fake.restore()

    assert code == 0
    output = buffer.getvalue()
    assert "[mobile]" in output
    assert "enabled = true" in output
