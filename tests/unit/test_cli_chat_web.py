"""`rings --web`: a convenience, never a second runtime (ADR-0041 §54).

The default `rings` behaviour is pinned here too, because the one thing this phase must not do is
change the terminal interaction while adding a browser one.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from assistant import cli_chat
from assistant.domain.config import AssistantConfig

CONFIG = "\n".join(
    (
        "format_version = 1",
        "",
        "[planning]",
        'timezone = "Asia/Shanghai"',
        "",
        "[mobile]",
        "enabled = true",
        'bind = "loopback"',
        "port = 8791",
        "",
    )
)

LAN_CONFIG = CONFIG.replace('bind = "loopback"', 'bind = "lan"')
DISABLED_CONFIG = CONFIG.replace("enabled = true", "enabled = false")


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG, encoding="utf-8")
    return path


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def _load(path: Path) -> AssistantConfig:
    """Load a host configuration the way the CLI does, from a synchronous entry point."""
    from assistant import bootstrap

    return asyncio.run(bootstrap.config_loader(path).load())


def test_the_url_is_the_configured_loopback_chat_page(config_file: Path) -> None:
    config = _load(config_file)

    assert cli_chat.web_chat_url(config) == "http://127.0.0.1:8791/chat"


def test_a_disabled_control_plane_has_no_url(config_file: Path, tmp_path: Path) -> None:
    config = _load(_write(tmp_path, DISABLED_CONFIG))

    assert cli_chat.web_chat_url(config) is None
    assert cli_chat.web_chat_url(None) is None


def test_a_lan_control_plane_is_reached_through_a_lan_address(tmp_path: Path) -> None:
    """The URL never contains a hostname a user typed; it is where the kernel says this host is."""
    config = _load(_write(tmp_path, LAN_CONFIG))

    url = cli_chat.web_chat_url(config)

    assert url is not None
    assert url.endswith(":8791/chat")
    assert url.startswith("http://")
    assert "://0.0.0.0" not in url


def test_a_reachable_control_plane_opens_the_browser_once(
    config_file: Path,
) -> None:
    opened: list[str] = []

    code = cli_chat.open_web_chat(
        config_path=config_file,
        opener=opened.append,
        probe=lambda url: True,
    )

    assert code == 0
    assert opened == ["http://127.0.0.1:8791/chat"]


def test_an_unreachable_control_plane_explains_itself(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    opened: list[str] = []

    code = cli_chat.open_web_chat(
        config_path=config_file,
        opener=opened.append,
        probe=lambda url: False,
    )

    output = capsys.readouterr().out
    assert code == 1
    assert opened == []
    assert "http://127.0.0.1:8791/chat" in output
    assert "assistantd" in output
    assert "rings" in output
    assert "Traceback" not in output


def test_a_disabled_control_plane_is_explained_without_opening_anything(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    opened: list[str] = []

    code = cli_chat.open_web_chat(
        config_path=_write(tmp_path, DISABLED_CONFIG),
        opener=opened.append,
        probe=lambda url: True,
    )

    output = capsys.readouterr().out
    assert code == 1
    assert opened == []
    assert "[mobile]" in output


def test_a_missing_configuration_is_a_sentence_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No config file means the documented defaults, and the default is "not enabled"."""
    code = cli_chat.open_web_chat(
        config_path=tmp_path / "absent" / "config.toml",
        opener=lambda url: None,
        probe=lambda url: True,
    )

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert code == 1
    assert "Traceback" not in output
    assert "[mobile]" in output


def test_an_unreadable_configuration_points_at_the_diagnostic_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "config.toml"
    broken.write_text(
        "format_version = 1\n\n[mobile]\nenabled = true\nbind = \"public\"\nport = 8791\n",
        encoding="utf-8",
    )

    code = cli_chat.open_web_chat(
        config_path=broken, opener=lambda url: None, probe=lambda url: True
    )

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert code == 1
    assert "Traceback" not in output
    assert "pw doctor" in output


def test_reachability_is_a_single_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused socket is simply "not running"; nothing is retried and nothing is started."""
    attempts: list[tuple[str, int]] = []

    def _refuse(address: tuple[str, int], timeout: float):
        attempts.append(address)
        raise OSError("connection refused")

    monkeypatch.setattr(cli_chat.socket, "create_connection", _refuse)

    assert cli_chat.reachable("http://127.0.0.1:8791/chat") is False
    assert attempts == [("127.0.0.1", 8791)]


def test_the_default_entry_point_still_opens_the_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str | None] = []
    monkeypatch.setattr(cli_chat.sys, "argv", ["rings"])
    monkeypatch.setattr(
        cli_chat, "run_conversation", lambda announce=None: calls.append(announce) or 0
    )

    with pytest.raises(SystemExit) as exit_code:
        cli_chat.main()

    assert exit_code.value.code == 0
    assert calls == ["Rings — 本地个人运营系统"]


def test_the_web_entry_point_dispatches_to_the_browser_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`rings --web` is a flag on the same entry point, and it returns that path's exit code."""
    calls: list[int] = []
    monkeypatch.setattr(cli_chat.sys, "argv", ["rings", "--web"])
    monkeypatch.setattr(cli_chat, "open_web_chat", lambda: calls.append(1) or 0)

    with pytest.raises(SystemExit) as exit_code:
        cli_chat.main()

    assert exit_code.value.code == 0
    assert calls == [1]


def test_an_unknown_argument_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli_chat.sys, "argv", ["rings", "--serve"])

    with pytest.raises(SystemExit) as exit_code:
        cli_chat.main()

    captured = capsys.readouterr()
    assert exit_code.value.code == 2
    assert "rings [--web]" in captured.out + captured.err
