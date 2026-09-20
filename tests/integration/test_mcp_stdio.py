"""The real stdio server: a subprocess, the official client, and a clean protocol stream (ADR-0030).

This is the only test that spawns the console script, and it is the one that proves the properties a
VS Code session actually depends on: the process starts, negotiates a session, enumerates its
surface, reads a resource, shuts down cleanly, and never writes a byte of prose into the stdout
stream that carries the protocol. The environment is a temporary XDG tree, so it neither reads nor
writes the developer's own configuration.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from assistant.adapters.mcp.resources import RESOURCE_URIS
from assistant.adapters.mcp.tools import GET_CASE_TOOL, GET_TASK_TOOL

CONFIG_TEMPLATE = """\
format_version = 1

[[storage.roots]]
kind = "local"
id = "university"
label = "University"
path = "{root}"
enabled = true

[mcp]
enabled = {enabled}
write_scope = "{write_scope}"
expose_knowledge = {expose_knowledge}
"""


def _console_script() -> str:
    """The installed `growing-assistant-mcp` script, or skip when it is not on this interpreter."""
    candidate = Path(sys.executable).parent / "growing-assistant-mcp"
    if not candidate.exists():  # pragma: no cover - depends on the environment
        pytest.skip("the console script is not installed in this environment")
    return str(candidate)


def _host(tmp_path: Path, **overrides: object) -> tuple[Path, Path]:
    """A temporary host: its own config tree, its own runtime data, its own watched directory."""
    documents = tmp_path / "documents"
    documents.mkdir(parents=True, exist_ok=True)
    config_home = tmp_path / "config"
    directory = config_home / "growing-assistant"
    directory.mkdir(parents=True, exist_ok=True)
    values: dict[str, object] = {
        "root": documents,
        "enabled": "true",
        "write_scope": "none",
        "expose_knowledge": "false",
    }
    values.update(overrides)
    (directory / "config.toml").write_text(
        CONFIG_TEMPLATE.format(**values), encoding="utf-8"
    )
    return config_home, tmp_path / "data"


def _parameters(
    config_home: Path, data_home: Path, *, path_prefix: str | None = None
) -> StdioServerParameters:
    """Server parameters with an isolated environment, never the developer's own."""
    environment = {
        "XDG_CONFIG_HOME": str(config_home),
        "XDG_DATA_HOME": str(data_home),
        "XDG_CACHE_HOME": str(data_home.parent / "cache"),
        "PATH": path_prefix or str(Path(sys.executable).parent) + ":/usr/bin:/bin",
    }
    return StdioServerParameters(
        command=_console_script(), args=[], env=environment, cwd=os.getcwd()
    )


async def test_the_stdio_server_negotiates_a_session_and_shuts_down_cleanly(
    tmp_path: Path,
) -> None:
    config_home, data_home = _host(tmp_path)

    async with Client(_parameters(config_home, data_home)) as client:
        server = client.server_info
        tools = [tool.name for tool in (await client.list_tools()).tools]
        resources = [str(item.uri) for item in (await client.list_resources()).resources]
        status = await client.read_resource("assistant://status")

    payload = json.loads(status.contents[0].text)  # type: ignore[attr-defined]

    assert server is not None and server.name == "growing-assistant"
    assert tools == [GET_TASK_TOOL, GET_CASE_TOOL]
    assert resources == list(RESOURCE_URIS)
    assert payload["mcp"]["transport"] == "stdio"
    assert payload["counts"]["open_tasks"] == 0


async def test_the_task_write_surface_appears_when_the_scope_allows_it(
    tmp_path: Path,
) -> None:
    config_home, data_home = _host(tmp_path, write_scope="tasks")

    async with Client(_parameters(config_home, data_home)) as client:
        tools = [tool.name for tool in (await client.list_tools()).tools]
        created = await client.call_tool(
            "assistant_create_task", {"title": "A task from the editor"}
        )
        listed = await client.read_resource("assistant://tasks/open")

    assert tools == [
        GET_TASK_TOOL,
        GET_CASE_TOOL,
        "assistant_create_task",
        "assistant_complete_task",
    ]
    assert created.is_error is False
    assert json.loads(listed.contents[0].text)["tasks"][0]["title"] == (  # type: ignore[attr-defined]
        "A task from the editor"
    )


async def test_the_write_tool_is_absent_over_real_stdio_when_the_scope_is_none(
    tmp_path: Path,
) -> None:
    config_home, data_home = _host(tmp_path, write_scope="none")

    async with Client(_parameters(config_home, data_home)) as client:
        result = await client.call_tool("assistant_create_task", {"title": "nope"})

    assert result.is_error is True


def test_a_disabled_server_exits_without_writing_to_stdout(tmp_path: Path) -> None:
    """§32: a disabled server must not emit protocol garbage, or prose, on stdout."""
    import subprocess

    config_home, data_home = _host(tmp_path, enabled="false")
    completed = subprocess.run(
        [_console_script()],
        env={
            "XDG_CONFIG_HOME": str(config_home),
            "XDG_DATA_HOME": str(data_home),
            "PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        stdin=subprocess.DEVNULL,
    )

    assert completed.returncode != 0
    assert completed.stdout == ""
    assert "disabled" in completed.stderr


def test_the_server_does_not_need_the_daemon(tmp_path: Path) -> None:
    """§33: the stdio server works with `assistantd` stopped, because it owns no daemon state."""
    import subprocess

    config_home, data_home = _host(tmp_path)
    environment = {
        "XDG_CONFIG_HOME": str(config_home),
        "XDG_DATA_HOME": str(data_home),
        "PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin",
    }
    # No daemon is running in this test environment; the server still builds its own database and
    # answers an initialize handshake before stdin closes.
    completed = subprocess.run(
        [_console_script()],
        env=environment,
        input='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}\n',
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode in (0, 1)
    first_line = completed.stdout.splitlines()[0] if completed.stdout else ""
    assert first_line.startswith("{")  # a JSON-RPC frame, never a log line
    assert "assistant.mcp" not in completed.stdout


def test_the_console_script_accepts_no_transport_arguments(tmp_path: Path) -> None:
    """§35: there is no `--transport http`, no `--port` and no `--host` to pass."""
    import subprocess

    config_home, data_home = _host(tmp_path)
    completed = subprocess.run(
        [_console_script(), "--transport", "http"],
        env={
            "XDG_CONFIG_HOME": str(config_home),
            "XDG_DATA_HOME": str(data_home),
            "PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        stdin=subprocess.DEVNULL,
    )

    assert completed.returncode != 0
    assert "transport" in completed.stderr.lower()
    assert completed.stdout == ""


def test_the_repository_installs_the_console_script() -> None:
    """The entry point is declared in the project metadata, not only in test assumptions."""
    import tomllib

    root = Path(__file__).resolve().parents[2]
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["project"]["scripts"]["growing-assistant-mcp"] == (
        "assistant.adapters.mcp.server:main"
    )
    assert shutil.which("growing-assistant-mcp", path=str(Path(sys.executable).parent))
