"""The one Startup-folder file, rendered and managed without touching the real folder (ADR-0046)."""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.adapters.runtime.windows_autostart import (
    AUTOSTART_FILENAME,
    AutostartSpec,
    _appdata_roaming,
    autostart_status,
    install_autostart,
    remove_autostart,
    render_autostart_cmd,
    windows_startup_directory,
)


def _spec(tmp_path: Path, **overrides: object) -> AutostartSpec:
    values: dict[str, object] = {
        "distro": "Debian",
        "user": "mrtree",
        "repo": Path("/home/mrtree/projects/growing-assistant"),
        "runtime_root": Path("/home/mrtree/.local/share/growing-assistant"),
        "uv": "/home/mrtree/.local/bin/uv",
    }
    values.update(overrides)
    return AutostartSpec(**values)  # type: ignore[arg-type]


def test_the_command_is_one_wsl_line_with_windows_line_endings(tmp_path: Path) -> None:
    rendered = render_autostart_cmd(_spec(tmp_path))

    assert rendered.endswith("\r\n")
    # Windows batch files want CRLF, and a bare LF must not survive anywhere in the file.
    assert "\r\n" in rendered
    assert "\n" not in rendered.replace("\r\n", "")
    assert "wsl.exe -d Debian -u mrtree -- bash -lc" in rendered
    assert "cd '/home/mrtree/projects/growing-assistant'" in rendered
    assert "'/home/mrtree/.local/bin/uv' run assistantd" in rendered
    assert ">> '/home/mrtree/.local/share/growing-assistant/assistantd.log' 2>&1" in rendered
    # A path with a space must still survive: the quoting above is what makes that true.
    spaced = render_autostart_cmd(_spec(tmp_path, repo=Path("/home/mrtree/my projects/rings")))
    assert "cd '/home/mrtree/my projects/rings'" in spaced


def test_install_status_and_remove_round_trip(tmp_path: Path) -> None:
    startup = tmp_path / "Startup"
    spec = _spec(tmp_path)

    assert autostart_status(startup_dir=startup).installed is False

    written = install_autostart(spec, startup_dir=startup)

    assert written == startup / AUTOSTART_FILENAME
    status = autostart_status(startup_dir=startup)
    assert status.installed is True
    assert status.content == render_autostart_cmd(spec)
    assert remove_autostart(startup_dir=startup) is True
    assert autostart_status(startup_dir=startup).installed is False
    assert remove_autostart(startup_dir=startup) is False  # idempotent


def test_installing_twice_replaces_the_file(tmp_path: Path) -> None:
    startup = tmp_path / "Startup"

    install_autostart(_spec(tmp_path), startup_dir=startup)
    second = _spec(tmp_path, distro="Ubuntu")
    install_autostart(second, startup_dir=startup)

    status = autostart_status(startup_dir=startup)
    assert status.installed is True
    assert "wsl.exe -d Ubuntu" in (status.content or "")


def test_the_startup_directory_comes_from_appdata_roaming(tmp_path: Path) -> None:
    appdata = tmp_path / "Roaming"

    resolved = windows_startup_directory(appdata=appdata)

    assert resolved == appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def test_a_non_utf8_cmd_answer_falls_back_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`cmd.exe` answers in the console code page; GBK bytes on a Chinese host must not raise."""
    import subprocess

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        # `echo %APPDATA%` on a Chinese Windows: the path is GBK, not UTF-8.
        gbk_path = b"C:\\Users\\\xd3\xc3\xbb\xa7\\AppData"
        return subprocess.CompletedProcess(argv, 0, stdout=gbk_path, stderr=b"")

    monkeypatch.setattr("assistant.adapters.runtime.windows_autostart.subprocess.run", fake_run)

    roaming = _appdata_roaming()  # must not raise

    assert roaming is None or isinstance(roaming, Path)


def test_a_missing_cmd_exe_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_oserror(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("cmd.exe")

    monkeypatch.setattr(
        "assistant.adapters.runtime.windows_autostart.subprocess.run", raise_oserror
    )

    assert _appdata_roaming() is None or isinstance(_appdata_roaming(), Path)
