"""Starting, waiting for and stopping the daemon process (ADR-0046).

`rings up` and `rings down` need four process-level answers, and nothing else in the project does:
is a daemon running for this runtime root, start one detached, wait until its control plane
answers, and stop one. They live here — an adapter, next to the instance lock and the permission
helpers — because they touch the process table, the socket layer and the log file.

Nothing here guesses: "is it running" comes from the same `flock` probe the daemon itself uses, and
the pid that gets signalled is the pid that holds the lock.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from assistant.adapters.runtime.instance_lock import daemon_lock_path, inspect_lock
from assistant.adapters.runtime.permissions import (
    ensure_private_directory,
    ensure_private_file,
)

LOG_FILENAME = "assistantd.log"
LOG_LIMIT_BYTES = 5 * 1024 * 1024
"""One previous log is kept; a daemon log is diagnostic material, not an archive."""

READINESS_TIMEOUT_SECONDS = 20.0
STOP_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True, slots=True)
class DaemonState:
    """What the lock says about the daemon, taken from the live holder's own metadata."""

    running: bool
    pid: int | None = None
    version: str | None = None
    started_at: str | None = None


@dataclass(frozen=True, slots=True)
class StopOutcome:
    """Whether the daemon actually stopped, and which pid was asked."""

    stopped: bool
    pid: int | None


def daemon_state(runtime_root: Path) -> DaemonState:
    """Whether a live daemon holds this runtime root's lock, and what it reported about itself."""
    state = inspect_lock(daemon_lock_path(Path(runtime_root)))
    if not state.held:
        return DaemonState(running=False)
    metadata = state.metadata
    pid = metadata.get("pid")
    return DaemonState(
        running=True,
        pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
        version=_text(metadata.get("version")),
        started_at=_text(metadata.get("started_at")),
    )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def daemon_log_path(runtime_root: Path) -> Path:
    """Where a detached daemon's output goes: the runtime directory, owner-only."""
    return Path(runtime_root) / LOG_FILENAME


def rotate_log(path: Path, *, limit_bytes: int = LOG_LIMIT_BYTES) -> bool:
    """Rename an oversized log to `<name>.1` once per start; return whether it rotated."""
    if not path.is_file() or path.stat().st_size <= limit_bytes:
        return False
    rotated = path.with_name(f"{path.name}.1")
    rotated.unlink(missing_ok=True)
    path.replace(rotated)
    ensure_private_file(rotated)
    return True


def assistantd_argv() -> list[str]:
    """How to start this installation's daemon: its console script, or the module."""
    script = Path(sys.executable).parent / "assistantd"
    if script.is_file():
        return [str(script)]
    found = shutil.which("assistantd")
    if found:
        return [found]
    return [sys.executable, "-c", "from assistant.daemon import main; main()"]


def spawn_daemon(runtime_root: Path, *, environ: Mapping[str, str] | None = None) -> int:
    """Start the daemon detached, logging into the runtime directory. Returns its pid.

    It is detached on purpose: closing the terminal that ran `rings up` must not stop Rings, and
    the child is the only long-lived holder of the instance lock.
    """
    root = Path(runtime_root)
    ensure_private_directory(root)
    log_path = daemon_log_path(root)
    rotate_log(log_path)
    handle = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        ensure_private_file(log_path)
        process = subprocess.Popen(
            assistantd_argv(),
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=handle,
            start_new_session=True,
            cwd=str(Path.cwd()),
            env=dict(os.environ if environ is None else environ),
        )
    finally:
        os.close(handle)
    return process.pid


def probe_http(url: str, *, timeout_seconds: float = 0.5) -> bool:
    """Whether the control plane answered *anything* at `url`.

    A `401` is a perfectly good answer: it proves the server is listening and merely does not know
    this browser yet. Only a connection-level failure counts as "not ready".
    """
    try:
        urllib.request.urlopen(url, timeout=timeout_seconds)
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return True


def stop_daemon(
    runtime_root: Path,
    *,
    timeout_seconds: float = STOP_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> StopOutcome:
    """Ask the lock holder to stop, and wait for the lock to be free.

    `SIGTERM` is the signal the daemon's own shutdown path already handles: services stop, the lock
    is released, and a turn that was mid-flight is recorded as interrupted rather than replayed.
    Nothing is killed forcefully here.
    """
    state = daemon_state(runtime_root)
    if not state.running or state.pid is None:
        return StopOutcome(stopped=False, pid=None)
    try:
        os.kill(state.pid, signal.SIGTERM)
    except ProcessLookupError:  # pragma: no cover - it exited between the probe and the signal
        return StopOutcome(stopped=True, pid=state.pid)
    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        if not daemon_state(runtime_root).running:
            return StopOutcome(stopped=True, pid=state.pid)
        sleep(0.2)
    return StopOutcome(stopped=False, pid=state.pid)


__all__ = [
    "LOG_FILENAME",
    "LOG_LIMIT_BYTES",
    "READINESS_TIMEOUT_SECONDS",
    "STOP_TIMEOUT_SECONDS",
    "DaemonState",
    "StopOutcome",
    "assistantd_argv",
    "daemon_log_path",
    "daemon_state",
    "probe_http",
    "rotate_log",
    "spawn_daemon",
    "stop_daemon",
]
