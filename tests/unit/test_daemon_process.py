"""The process side of the launcher, tested without starting a daemon (ADR-0046)."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

import pytest

from assistant.adapters.runtime.daemon_process import (
    DaemonState,
    StopOutcome,
    assistantd_argv,
    daemon_log_path,
    daemon_state,
    rotate_log,
    stop_daemon,
)
from assistant.adapters.runtime.instance_lock import daemon_lock_path


def test_no_lock_file_means_not_running_and_creates_nothing(tmp_path: Path) -> None:
    root = tmp_path / "data" / "growing-assistant"
    root.mkdir(parents=True)

    state = daemon_state(root)

    assert state == DaemonState(running=False)
    assert daemon_lock_path(root).exists() is False


def test_a_held_lock_reports_the_holder(tmp_path: Path) -> None:
    root = tmp_path / "data" / "growing-assistant"
    root.mkdir(parents=True)
    lock_path = daemon_lock_path(root)
    lock_path.write_text(
        json.dumps(
            {"pid": os.getpid(), "version": "1.4.0", "started_at": "2026-09-22T10:00:00+00:00"}
        ),
        encoding="utf-8",
    )
    handle = os.open(lock_path, os.O_RDWR)
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        state = daemon_state(root)
    finally:
        os.close(handle)

    assert state.running is True
    assert state.pid == os.getpid()
    assert state.version == "1.4.0"
    assert state.started_at == "2026-09-22T10:00:00+00:00"


def test_a_lock_without_metadata_still_reports_running(tmp_path: Path) -> None:
    """A held lock is the authority; the metadata beside it is a courtesy."""
    root = tmp_path / "data" / "growing-assistant"
    root.mkdir(parents=True)
    lock_path = daemon_lock_path(root)
    lock_path.write_text("not json", encoding="utf-8")
    handle = os.open(lock_path, os.O_RDWR)
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        state = daemon_state(root)
    finally:
        os.close(handle)

    assert state.running is True
    assert state.pid is None
    assert state.version is None


def test_stop_daemon_reports_nothing_running(tmp_path: Path) -> None:
    root = tmp_path / "data" / "growing-assistant"
    root.mkdir(parents=True)

    assert stop_daemon(root) == StopOutcome(stopped=False, pid=None)


def test_stop_daemon_signals_the_holder_and_waits_for_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data" / "growing-assistant"
    root.mkdir(parents=True)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "assistant.adapters.runtime.daemon_process.daemon_state",
        lambda _root: DaemonState(running=True, pid=4242),
    )
    monkeypatch.setattr(
        "assistant.adapters.runtime.daemon_process.os.kill",
        lambda pid, sig: signals.append((pid, sig)),
    )
    ticks = iter([0.0, 0.0, 0.1, 0.2, 2.0])

    outcome = stop_daemon(
        root, timeout_seconds=1.0, sleep=lambda _s: None, monotonic=lambda: next(ticks)
    )

    assert signals == [(4242, 15)]  # SIGTERM, exactly once
    assert outcome == StopOutcome(stopped=False, pid=4242)  # it never became free in the window


def test_stop_daemon_reports_success_once_the_lock_is_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data" / "growing-assistant"
    root.mkdir(parents=True)
    calls: list[int] = []

    def state(_root: Path) -> DaemonState:
        calls.append(1)
        return DaemonState(running=True, pid=7) if len(calls) == 1 else DaemonState(running=False)

    monkeypatch.setattr("assistant.adapters.runtime.daemon_process.daemon_state", state)
    monkeypatch.setattr(
        "assistant.adapters.runtime.daemon_process.os.kill", lambda _pid, _sig: None
    )

    outcome = stop_daemon(root, timeout_seconds=5.0, sleep=lambda _s: None)

    assert outcome == StopOutcome(stopped=True, pid=7)


def test_rotation_keeps_one_previous_log(tmp_path: Path) -> None:
    log = daemon_log_path(tmp_path / "runtime")
    log.parent.mkdir(parents=True)
    log.write_text("old\n", encoding="utf-8")

    assert rotate_log(log, limit_bytes=1) is True
    assert log.exists() is False
    assert (log.parent / "assistantd.log.1").read_text(encoding="utf-8") == "old\n"

    log.write_text("small\n", encoding="utf-8")
    assert rotate_log(log, limit_bytes=10_000) is False
    assert log.read_text(encoding="utf-8") == "small\n"


def test_rotation_replaces_the_previous_rotated_file(tmp_path: Path) -> None:
    log = daemon_log_path(tmp_path / "runtime")
    log.parent.mkdir(parents=True)
    (log.parent / "assistantd.log.1").write_text("older\n", encoding="utf-8")
    log.write_text("old\n", encoding="utf-8")

    assert rotate_log(log, limit_bytes=1) is True

    assert (log.parent / "assistantd.log.1").read_text(encoding="utf-8") == "old\n"


def test_the_command_is_the_console_script_when_this_environment_has_one() -> None:
    argv = assistantd_argv()

    assert argv
    # Either the console script next to this interpreter, or the module fallback — both start the
    # real daemon, and neither is a user-supplied string.
    assert argv[0].endswith("assistantd") or "assistant.daemon" in " ".join(argv)
