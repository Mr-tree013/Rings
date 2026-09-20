"""`pw ehall` — the CLI surface, and the commands it deliberately does not have (ADR-0025)."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from assistant import bootstrap
from assistant.adapters.ehall.nju_certificate import NjuCertificateGateway
from assistant.cli import app
from assistant.cli_ehall import certificate_app, ehall_app
from tests.support.ehall import (
    FIELD_KEY,
    SECOND_FIELD_KEY,
    FakeEHallPage,
    standard_form,
)

runner = CliRunner()
NOW = datetime(2026, 9, 24, 9, 0, tzinfo=UTC)

ENABLED_CONFIG = "\n".join(
    (
        "format_version = 1",
        "",
        "[ehall]",
        "enabled = true",
        "timeout_seconds = 30",
        "",
    )
)


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary host with the pipeline enabled, and a fake page behind it."""
    config_home = tmp_path / "config"
    directory = config_home / "growing-assistant"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.toml").write_text(ENABLED_CONFIG, encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "browsers"))
    return tmp_path


def _install_fake_pipeline(
    monkeypatch: pytest.MonkeyPatch, page: FakeEHallPage | None = None
) -> FakeEHallPage:
    """Replace the browser entirely: the CLI is tested against the pipeline's own policy."""
    scripted = page or FakeEHallPage()

    class _Session:
        async def start(self) -> None:
            return None

        async def close(self) -> None:
            return None

    def _gateway(config: object = None, **kwargs: object) -> NjuCertificateGateway:
        async def _open(session: object) -> FakeEHallPage:
            del session
            return scripted

        return NjuCertificateGateway(
            lambda: _Session(),  # type: ignore[arg-type,return-value]
            _open,  # type: ignore[arg-type]
            enabled=bool(kwargs.get("enabled", True)),
            timeout_seconds=30,
        )

    monkeypatch.setattr(bootstrap, "ehall_certificate_gateway", _gateway)
    return scripted


def _case(title: str = "Apply for a certificate") -> str:
    result = runner.invoke(app, ["case", "add", title])
    assert result.exit_code == 0, result.output
    match = re.search(r"\b([0-9a-f]{8})\b", result.output)
    assert match is not None, result.output
    return match.group(1)


# -------------------------------------------------------------------- commands


def test_status_is_local_and_honest(isolated: Path) -> None:
    """It reports the capability, and never claims the saved session is still valid."""
    result = runner.invoke(app, ["ehall", "status"])

    assert result.exit_code == 0, result.output
    assert "Enabled" in result.output and "yes" in result.output
    assert "Playwright package" in result.output and "installed" in result.output
    assert "Chromium runtime" in result.output
    assert "profile" in result.output
    assert "certificate application" in result.output
    assert "Executor registered" in result.output
    assert "logged in" in result.output  # explicitly disclaims a live session check


def test_status_reports_a_disabled_pipeline(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = Path(isolated) / "config" / "growing-assistant"
    (directory / "config.toml").write_text(
        ENABLED_CONFIG.replace("enabled = true", "enabled = false"), encoding="utf-8"
    )

    result = runner.invoke(app, ["ehall", "status"])

    assert result.exit_code == 0, result.output
    assert "no (pipeline disabled)" in result.output


def test_inspect_prints_the_contract_without_submitting(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = _install_fake_pipeline(monkeypatch)

    result = runner.invoke(app, ["ehall", "certificate", "inspect"])

    assert result.exit_code == 0, result.output
    assert "nju-ehall" in result.output
    assert "身份证件" in result.output  # required materials
    assert FIELD_KEY in result.output and SECOND_FIELD_KEY in result.output
    assert "在读证明" in result.output  # allowed options
    assert "page contract fingerprint" in result.output
    assert "Nothing was typed and nothing was submitted." in result.output
    assert page.fills == [] and page.clicks == []


def test_inspect_reports_a_missing_login(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_pipeline(
        monkeypatch, FakeEHallPage(form=standard_form(session_expired=True))
    )

    result = runner.invoke(app, ["ehall", "certificate", "inspect"])

    assert result.exit_code == 1
    assert "pw ehall login" in result.output


def test_inspect_reports_a_required_upload(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assistant.adapters.ehall.nju_certificate import RawField

    _install_fake_pipeline(
        monkeypatch,
        FakeEHallPage(
            form=standard_form(
                fields=(
                    RawField(key=FIELD_KEY, label="申请人姓名", kind="text", required=True),
                    RawField(key=None, label="身份证件扫描件", kind="file", required=True),
                )
            )
        ),
    )

    result = runner.invoke(app, ["ehall", "certificate", "inspect"])

    assert result.exit_code == 1
    assert "cannot fill" in result.output


def test_prepare_freezes_the_action_and_does_not_submit(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = _install_fake_pipeline(monkeypatch)
    case = _case()

    result = runner.invoke(
        app,
        [
            "ehall",
            "certificate",
            "prepare",
            "--case",
            case,
            "--field",
            f"{FIELD_KEY}=张同学",
            "--field",
            f"{SECOND_FIELD_KEY}=在读证明",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Prepared exact eHall certificate action." in result.output
    assert "Nothing was submitted." in result.output
    assert "pw action challenge" in result.output
    assert "Fingerprint:" in result.output
    assert "Consequence" in result.output and "administrative record" in result.output
    assert "张同学" in result.output
    assert page.fills == [] and page.clicks == []


def test_prepare_refuses_a_missing_required_field_or_a_bad_value(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_pipeline(monkeypatch)
    case = _case()

    missing = runner.invoke(
        app,
        ["ehall", "certificate", "prepare", "--case", case, "--field", f"{FIELD_KEY}=张"],
    )
    bad_option = runner.invoke(
        app,
        [
            "ehall",
            "certificate",
            "prepare",
            "--case",
            case,
            "--field",
            f"{FIELD_KEY}=张",
            "--field",
            f"{SECOND_FIELD_KEY}=毕业证明",
        ],
    )
    malformed = runner.invoke(
        app,
        ["ehall", "certificate", "prepare", "--case", case, "--field", "nonsense"],
    )
    unknown_key = runner.invoke(
        app,
        [
            "ehall",
            "certificate",
            "prepare",
            "--case",
            case,
            "--field",
            f"{FIELD_KEY}=张",
            "--field",
            f"{SECOND_FIELD_KEY}=在读证明",
            "--field",
            "nickname=x",
        ],
    )

    assert missing.exit_code == 1 and "required" in missing.output
    assert bad_option.exit_code == 1 and "must be one of" in bad_option.output
    assert malformed.exit_code == 1 and "KEY=VALUE" in malformed.output
    assert unknown_key.exit_code == 1 and "unknown field" in unknown_key.output


def test_prepare_reports_a_closed_case(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_pipeline(monkeypatch)
    case = _case()
    runner.invoke(app, ["case", "done", case])

    result = runner.invoke(
        app,
        [
            "ehall",
            "certificate",
            "prepare",
            "--case",
            case,
            "--field",
            f"{FIELD_KEY}=张",
        ],
    )

    assert result.exit_code == 1
    assert "cannot receive a new action" in result.output


def test_show_prints_the_critical_preview_and_touches_nothing(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = _install_fake_pipeline(monkeypatch)
    case = _case()
    prepared = runner.invoke(
        app,
        [
            "ehall",
            "certificate",
            "prepare",
            "--case",
            case,
            "--field",
            f"{FIELD_KEY}=张同学",
            "--field",
            f"{SECOND_FIELD_KEY}=在读证明",
        ],
    )
    action_id = re.search(r"Action:\s+([0-9a-f-]{36})", prepared.output)
    assert action_id is not None, prepared.output
    page.opened.clear()

    shown = runner.invoke(
        app, ["ehall", "certificate", "show", action_id.group(1)[:8]]
    )

    assert shown.exit_code == 0, shown.output
    assert "Service" in shown.output and "nju-ehall" in shown.output
    assert "Critical" in shown.output or "critical fields" in shown.output
    assert "张同学" in shown.output
    assert "Consequence" in shown.output
    assert "Approval" in shown.output and "Execution" in shown.output
    assert "Nothing is submitted by this command." in shown.output
    assert page.opened == []  # showing a prepared action opens no browser


def test_show_refuses_an_action_that_is_not_a_certificate(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_pipeline(monkeypatch)
    case = _case()
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    service = bootstrap.case_service(clock, database)
    case_id = asyncio.run(service.resolve_case_id(case))
    other = asyncio.run(
        service.prepare_action(case_id, "mail.send", {"to": "a@example.edu"})
    )

    result = runner.invoke(app, ["ehall", "certificate", "show", str(other.id)[:8]])

    assert result.exit_code == 1
    assert "not a certificate submission" in result.output


# ------------------------------------------------------------------ no surface


def test_there_is_no_command_that_submits_or_clicks() -> None:
    """The only submission path is `pw action execute`, after an approval."""
    names = {command.name for command in ehall_app.registered_commands}
    assert names == {"login", "status"}
    certificate_commands = {
        command.name for command in certificate_app.registered_commands
    }
    assert certificate_commands == {"inspect", "prepare", "show"}
    # The top-level group offers exactly login and status; a submit command does not exist.
    assert "submit" not in names and "click" not in names and "fill" not in names


def test_doctor_fails_when_the_pipeline_is_enabled_but_the_browser_is_missing(
    isolated: Path,
) -> None:
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "chromium" in result.output.lower()
    assert "uv run playwright install chromium" in result.output


def test_doctor_is_healthy_without_the_pipeline(isolated: Path) -> None:
    directory = Path(isolated) / "config" / "growing-assistant"
    (directory / "config.toml").write_text(
        "format_version = 1\n", encoding="utf-8"
    )

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "environment looks usable" in result.output
    assert "not configured" in result.output
