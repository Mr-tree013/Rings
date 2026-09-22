"""Starting `assistantd` when Windows logs in: one reversible file (ADR-0046).

A WSL distro here has no user-level systemd (`systemctl --user` is offline), and installing a
service would need privileges the user should not have to grant. So autostart is the same thing
Windows itself uses for "run this when I log in": a single `.cmd` in the per-user Startup folder,
which the user can inspect, keep, or delete with one command.
"""

from __future__ import annotations

import os
import pwd
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

AUTOSTART_FILENAME = "rings-assistantd.cmd"
"""The file's name in the Startup folder. One file, and its name says what it does."""

_STARTUP_TAIL = Path("Microsoft/Windows/Start Menu/Programs/Startup")


@dataclass(frozen=True, slots=True)
class AutostartSpec:
    """Everything the generated command needs, resolved rather than guessed."""

    distro: str
    user: str
    repo: Path
    runtime_root: Path
    uv: str


@dataclass(frozen=True, slots=True)
class AutostartStatus:
    """What is installed right now."""

    startup_dir: Path
    path: Path
    installed: bool
    content: str | None = None


def render_autostart_cmd(spec: AutostartSpec) -> str:
    """The exact file content, with Windows line endings and quoting that survives spaces.

    The quoted strings are *Linux* paths: `bash -lc` runs inside WSL, so `cd`, `uv` and the log
    redirection are all resolved there.
    """
    command = (
        f"wsl.exe -d {spec.distro} -u {spec.user} -- bash -lc "
        f"\"cd '{spec.repo}' && '{spec.uv}' run assistantd "
        f">> '{spec.runtime_root}/assistantd.log' 2>&1\""
    )
    return "\r\n".join(["@echo off", command, ""])


def windows_startup_directory(*, appdata: Path | None = None) -> Path | None:
    """The per-user Startup folder, or `None` when this is not a Windows-visible machine."""
    roaming = Path(appdata) if appdata is not None else _appdata_roaming()
    return None if roaming is None else roaming / _STARTUP_TAIL


def _run_text(argv: list[str], *, timeout: float = 10.0) -> str | None:
    """Run a Windows-side helper and return its output, or `None` when it cannot be read.

    The output is decoded tolerantly on purpose: `cmd.exe` answers in the console code page (GBK
    on a Chinese host, CP1252 elsewhere), so decoding as UTF-8 can fail — and a path this process
    cannot read is a reason to fall back, not a reason to crash the command.
    """
    try:
        completed = subprocess.run(argv, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.decode("utf-8", errors="replace").strip()


def _appdata_roaming() -> Path | None:
    """`%APPDATA%`, translated to a WSL path, with a single-user fallback."""
    raw = _run_text(["cmd.exe", "/c", "echo", "%APPDATA%"])
    if raw and "%APPDATA%" not in raw:
        translated = _run_text(["wslpath", "-u", raw])
        if translated:
            return Path(translated)
    candidates = sorted(Path("/mnt/c/Users").glob("*/AppData/Roaming"))
    return candidates[0] if len(candidates) == 1 else None


def current_distro() -> str | None:
    """The WSL distribution this process runs in."""
    named = os.environ.get("WSL_DISTRO_NAME", "").strip()
    if named:
        return named
    output = _run_text(["wsl.exe", "-l", "-q"])
    names = [line.strip() for line in (output or "").splitlines() if line.strip()]
    return names[0] if len(names) == 1 else None


def current_user() -> str:
    """This process's user name, from the password database rather than an environment variable."""
    return pwd.getpwuid(os.getuid()).pw_name


def resolved_uv() -> str:
    """The absolute `uv` to embed, so a non-interactive login shell does not need it on PATH."""
    return shutil.which("uv") or "uv"


def autostart_status(*, startup_dir: Path) -> AutostartStatus:
    """Whether the Startup file exists, and what it says."""
    directory = Path(startup_dir)
    path = directory / AUTOSTART_FILENAME
    if not path.is_file():
        return AutostartStatus(startup_dir=directory, path=path, installed=False)
    return AutostartStatus(
        startup_dir=directory,
        path=path,
        installed=True,
        # `newline=""`: show exactly what is on disk, CRLF and all — this is a Windows batch file.
        content=path.read_text(encoding="utf-8", newline=""),
    )


def install_autostart(spec: AutostartSpec, *, startup_dir: Path) -> Path:
    """Write the Startup file. An existing file is replaced deliberately."""
    directory = Path(startup_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / AUTOSTART_FILENAME
    path.write_text(render_autostart_cmd(spec), encoding="utf-8", newline="")
    return path


def remove_autostart(*, startup_dir: Path) -> bool:
    """Delete the Startup file; `False` when there was nothing to delete."""
    path = Path(startup_dir) / AUTOSTART_FILENAME
    if not path.is_file():
        return False
    path.unlink()
    return True


__all__ = [
    "AUTOSTART_FILENAME",
    "AutostartSpec",
    "AutostartStatus",
    "autostart_status",
    "current_distro",
    "current_user",
    "install_autostart",
    "remove_autostart",
    "render_autostart_cmd",
    "resolved_uv",
    "windows_startup_directory",
]
