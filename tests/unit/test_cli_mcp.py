"""`pw mcp status` and `pw mcp vscode-config`: describing the MCP surface (ADR-0030).

Both commands are local and read-only. `status` prints the surface a client would actually discover
(it asks the same registration functions the server uses), and `vscode-config` prints a snippet
without touching the user's editor configuration — which the tests check by looking for the file
afterwards.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from assistant.cli import app

runner = CliRunner()

BASE = "format_version = 1\n"


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("COLUMNS", "200")
    return tmp_path


def _write_config(isolated: Path, body: str) -> None:
    directory = isolated / "config" / "growing-assistant"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.toml").write_text(BASE + body, encoding="utf-8")


MCP_BLOCK = '[mcp]\nenabled = true\nwrite_scope = "none"\nexpose_knowledge = false\n'


def test_status_reports_stdio_and_the_default_surface(isolated: Path) -> None:
    _write_config(isolated, MCP_BLOCK)

    result = runner.invoke(app, ["mcp", "status"])

    assert result.exit_code == 0, result.output
    assert "Enabled" in result.output and "yes" in result.output
    assert "stdio" in result.output
    assert "Write scope" in result.output and "none" in result.output
    for uri in (
        "assistant://status",
        "assistant://tasks/open",
        "assistant://cases/open",
        "assistant://plan/current",
    ):
        assert uri in result.output
    assert "assistant_get_task" in result.output
    assert "assistant_get_case" in result.output
    assert "assistant_create_task" not in result.output
    assert "assistant_search_knowledge" not in result.output
    assert "a client cannot create or complete a task" in result.output
    assert "no approval, execution, mail, eHall" in result.output


def test_status_reflects_the_configured_scope(isolated: Path) -> None:
    _write_config(
        isolated,
        '[mcp]\nenabled = true\nwrite_scope = "tasks"\nexpose_knowledge = true\n',
    )

    result = runner.invoke(app, ["mcp", "status"])

    assert result.exit_code == 0, result.output
    assert "assistant_create_task" in result.output
    assert "assistant_complete_task" in result.output
    assert "assistant_search_knowledge" in result.output
    assert "Knowledge exposure" in result.output


def test_status_says_so_when_the_server_is_disabled(isolated: Path) -> None:
    _write_config(isolated, "[mcp]\nenabled = false\n")

    result = runner.invoke(app, ["mcp", "status"])

    assert result.exit_code == 0, result.output
    assert "no" in result.output
    assert "growing-assistant-mcp` exits without serving" in result.output


def test_status_works_with_no_configuration_at_all(isolated: Path) -> None:
    result = runner.invoke(app, ["mcp", "status"])

    assert result.exit_code == 0, result.output
    assert "stdio" in result.output
    assert "none" in result.output


def test_vscode_config_prints_a_stdio_snippet(isolated: Path) -> None:
    _write_config(isolated, MCP_BLOCK)

    result = runner.invoke(app, ["mcp", "vscode-config"])

    assert result.exit_code == 0, result.output
    start = result.output.index("{")
    end = result.output.rindex("}") + 1
    snippet = json.loads(result.output[start:end])
    server = snippet["servers"]["growing-assistant"]
    assert server["type"] == "stdio"
    assert server["command"] == "uv"
    assert server["args"] == ["run", "growing-assistant-mcp"]
    assert server["cwd"]
    assert "trust" in result.output
    assert "does not write it for you" in result.output


def test_vscode_config_contains_no_secret(isolated: Path) -> None:
    _write_config(isolated, MCP_BLOCK)

    result = runner.invoke(app, ["mcp", "vscode-config"])

    for forbidden in ("API_KEY", "PASSWORD", "token", "secret"):
        assert forbidden not in result.output.split("Paste this")[0]


def test_vscode_config_writes_nothing_to_the_editor(isolated: Path) -> None:
    """§37: the snippet is printed, never installed."""
    _write_config(isolated, MCP_BLOCK)

    result = runner.invoke(app, ["mcp", "vscode-config"])

    assert result.exit_code == 0, result.output
    assert not (isolated / ".vscode").exists()
    assert not (isolated / "config" / "Code").exists()
    assert not list(isolated.rglob("mcp.json"))


def test_vscode_config_honours_an_explicit_project_root(isolated: Path) -> None:
    _write_config(isolated, MCP_BLOCK)
    root = isolated / "somewhere" / "project"
    root.mkdir(parents=True)

    result = runner.invoke(
        app, ["mcp", "vscode-config", "--project-root", str(root)]
    )

    assert result.exit_code == 0, result.output
    start = result.output.index("{")
    end = result.output.rindex("}") + 1
    snippet = json.loads(result.output[start:end])
    assert snippet["servers"]["growing-assistant"]["cwd"] == str(root)


def test_the_generator_never_claims_to_configure_a_transport(isolated: Path) -> None:
    _write_config(isolated, MCP_BLOCK)

    result = runner.invoke(app, ["mcp", "vscode-config"])

    assert "http" not in result.output.replace("https", "")
    assert "--port" not in result.output
    assert "--host" not in result.output
