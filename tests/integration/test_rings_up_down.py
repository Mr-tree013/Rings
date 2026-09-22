"""`rings up` / `rings down` against a real daemon in a temporary XDG root (ADR-0046).

The configuration has no control plane, so nothing in this module needs a socket: readiness is the
instance lock. The HTTP readiness probe and the auto-pair URL are covered by the browser module,
which is already allowed loopback.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from assistant.adapters.runtime.daemon_process import daemon_state

CONFIG = """format_version = 1

[indexing]
interval_seconds = 3600
run_on_startup = false

[scheduler]
poll_interval_seconds = 300
replan_debounce_seconds = 60
"""

SHUTDOWN_TIMEOUT_SECONDS = 20.0


def _environment(tmp_path: Path) -> dict[str, str]:
    config = tmp_path / "config" / "growing-assistant"
    config.mkdir(parents=True, exist_ok=True)
    (config / "config.toml").write_text(CONFIG, encoding="utf-8")
    environment = dict(os.environ)
    environment.update(
        {
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "PYTHONUNBUFFERED": "1",
        }
    )
    environment.pop("DEEPSEEK_API_KEY", None)
    return environment


def _rings_command() -> list[str]:
    script = Path(sys.executable).parent / "rings"
    if script.is_file():
        return [str(script)]
    return [sys.executable, "-c", "from assistant.cli_chat import main; main()"]


def _run(tmp_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*_rings_command(), *arguments],
        env=_environment(tmp_path),
        capture_output=True,
        text=True,
        timeout=90,
    )


def _wait_until(predicate, *, timeout: float = SHUTDOWN_TIMEOUT_SECONDS) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return predicate()


def test_up_starts_a_daemon_and_down_stops_it(tmp_path: Path) -> None:
    root = tmp_path / "data" / "growing-assistant"

    up = _run(tmp_path, "up", "--no-open")
    try:
        assert up.returncode == 0, up.stderr
        assert "已启动" in up.stdout
        assert "没有配置账号" in up.stdout  # the status block reports the real configuration
        assert daemon_state(root).running is True
        assert (root / "assistantd.log").is_file()

        again = _run(tmp_path, "up", "--no-open")
        assert again.returncode == 0, again.stderr
        assert "已在运行" in again.stdout
    finally:
        down = _run(tmp_path, "down")

    assert down.returncode == 0, down.stderr
    assert "已停止" in down.stdout
    assert _wait_until(lambda: daemon_state(root).running is False)


def test_down_without_a_daemon_is_not_an_error(tmp_path: Path) -> None:
    down = _run(tmp_path, "down")

    assert down.returncode == 0, down.stderr
    assert "没有在运行" in down.stdout
