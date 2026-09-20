"""One daemon per runtime root, and a real process lifecycle (ADR-0032 §3/§5/§6/§7/§37/§38).

The guarantee is an OS advisory lock, so the tests use the real thing rather than a stub: an
in-process lock holder for the semantics (a second instance is refused, a stale file is not), and a
real `assistantd` subprocess for the lifecycle (it stays alive, holds the lock, is refused, stops on
`SIGTERM` and releases the lock so a successor can start).

Every wait is bounded and every wait is for an observable state — the process being alive, the lock
being held, the exit status — so nothing here depends on a fixed sleep.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from assistant import __version__
from assistant.adapters.runtime.instance_lock import (
    InstanceLock,
    InstanceLockHeld,
    daemon_lock_path,
    inspect_lock,
    read_lock_metadata,
)
from assistant.adapters.runtime.permissions import PRIVATE_FILE_MODE, file_mode

STARTUP_TIMEOUT_SECONDS = 20.0
SHUTDOWN_TIMEOUT_SECONDS = 20.0

CONFIG = """format_version = 1

[indexing]
interval_seconds = 10
run_on_startup = true

[scheduler]
poll_interval_seconds = 15
replan_debounce_seconds = 60
"""


def _runtime_root(tmp_path: Path) -> Path:
    root = tmp_path / "data" / "growing-assistant"
    root.mkdir(parents=True, exist_ok=True)
    return root


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
    # No provider, no mail account, no watcher: the daemon runs index-sync and the scheduler only.
    environment.pop("DEEPSEEK_API_KEY", None)
    return environment


def _daemon_command() -> list[str]:
    """The real console script when the environment has one, else the module entry point."""
    script = Path(sys.executable).parent / "assistantd"
    if script.is_file():
        return [str(script)]
    return [sys.executable, "-c", "from assistant.daemon import main; main()"]


def _wait_for(predicate, *, timeout: float = STARTUP_TIMEOUT_SECONDS) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


# ------------------------------------------------------------------ lock semantics (in process)


def test_a_second_instance_is_refused_while_the_first_holds_the_lock(tmp_path: Path) -> None:
    """§37: B is refused before it starts anything, and C succeeds once A has released."""
    lock_path = daemon_lock_path(_runtime_root(tmp_path))
    first = InstanceLock(lock_path, metadata={"pid": os.getpid(), "version": "test"}).acquire()
    try:
        with pytest.raises(InstanceLockHeld) as refused:
            InstanceLock(lock_path).acquire()
        assert "already running" in str(refused.value)
        assert inspect_lock(lock_path).held is True
    finally:
        first.release()

    third = InstanceLock(lock_path).acquire()
    try:
        assert third.held is True
    finally:
        third.release()
    assert inspect_lock(lock_path).held is False


def test_a_stale_lock_file_never_prevents_startup(tmp_path: Path) -> None:
    """§6: a lock file without a held lock is an old file, not an obstacle."""
    lock_path = daemon_lock_path(_runtime_root(tmp_path))
    lock_path.write_text('{"pid": 999999, "version": "0.1.0"}', encoding="utf-8")

    state = inspect_lock(lock_path)
    acquired = InstanceLock(lock_path).acquire()
    try:
        assert state.held is False
        assert state.metadata == {"pid": 999999, "version": "0.1.0"}  # informational only
        assert acquired.held is True
    finally:
        acquired.release()


def test_the_lock_file_is_private_and_diagnostic(tmp_path: Path) -> None:
    """§8: the lock is `0600` and carries only identity, never a credential."""
    lock_path = daemon_lock_path(_runtime_root(tmp_path))
    lock = InstanceLock(
        lock_path,
        metadata={
            "pid": os.getpid(),
            "started_at": "2026-09-26T09:00:00+00:00",
            "runtime_root": str(tmp_path),
            "version": "1.0.0",
        },
    ).acquire()
    try:
        assert file_mode(lock_path) == PRIVATE_FILE_MODE
        metadata = read_lock_metadata(lock_path)
        assert metadata["pid"] == os.getpid()
        assert metadata["version"] == "1.0.0"
        assert set(metadata) == {"pid", "started_at", "runtime_root", "version"}
    finally:
        lock.release()


# ------------------------------------------------------------- real subprocess lifecycle (§38)


def test_a_real_daemon_holds_the_lock_and_a_second_one_is_refused(tmp_path: Path) -> None:
    """Launch the real `assistantd`, watch the lock, refuse a sibling, stop it cleanly."""
    environment = _environment(tmp_path)
    lock_path = daemon_lock_path(_runtime_root(tmp_path))
    command = _daemon_command()
    first = subprocess.Popen(
        command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        assert _wait_for(lambda: inspect_lock(lock_path).held)
        assert read_lock_metadata(lock_path)["pid"] == first.pid

        second = subprocess.run(
            command, env=environment, capture_output=True, text=True, timeout=60
        )
        assert second.returncode != 0
        assert "already running" in second.stderr
        assert first.poll() is None  # the running daemon was not disturbed

        first.send_signal(signal.SIGTERM)
        _out, err = first.communicate(timeout=SHUTDOWN_TIMEOUT_SECONDS)
    finally:
        if first.poll() is None:  # pragma: no cover - only on a test failure
            first.kill()
            first.communicate(timeout=SHUTDOWN_TIMEOUT_SECONDS)

    assert first.returncode == 0, err
    assert inspect_lock(lock_path).held is False
    assert lock_path.is_file()  # the file stays; the lock does not

    successor = subprocess.Popen(
        command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        assert _wait_for(lambda: inspect_lock(lock_path).held)
    finally:
        successor.send_signal(signal.SIGTERM)
        successor.communicate(timeout=SHUTDOWN_TIMEOUT_SECONDS)
    assert successor.returncode == 0
    assert inspect_lock(lock_path).held is False


def test_the_daemon_reports_its_version_without_starting(tmp_path: Path) -> None:
    """§26: `assistantd --version` prints the version and opens nothing."""
    environment = _environment(tmp_path)
    lock_path = daemon_lock_path(_runtime_root(tmp_path))

    completed = subprocess.run(
        [*_daemon_command(), "--version"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0
    assert completed.stdout.strip() == f"assistantd {__version__}"
    assert not lock_path.exists()
    assert not (tmp_path / "data" / "growing-assistant" / "assistant.db").exists()
