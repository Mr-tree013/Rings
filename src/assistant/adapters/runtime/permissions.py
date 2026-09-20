"""Owner-only permissions for the private objects this project creates (ADR-0032).

Three rules, and the third is the important one:

* **directories the project creates are `0700`** — the runtime root, mail raw directories, web
  snapshot directories, the eHall profile, the knowledge-index cache, restore staging;
* **files the project creates are `0600`** — the runtime database and its snapshot, raw mail
  objects, web snapshots, backup archives and their temporaries, the daemon lock, restored content;
* **nothing else is ever touched.** A file the user already had — a vault document, a local
  knowledge root, the configuration file, an existing directory with modes the user chose — is
  reported by `pw integrity check` and left alone. Silently rewriting modes on somebody else's files
  is not hardening, it is a surprise.

The helpers are deliberately best-effort and platform-aware: on a filesystem or platform without
Unix mode bits (`os.chmod` raising, or the mode simply not sticking) the call is a no-op instead of
a startup failure. What the project can *check* it reports; what it cannot enforce it does not
pretend to.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from assistant.ports.file_modes import ObjectMode

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600

_GROUP_OR_OTHER_BITS = 0o077
_WORLD_WRITE_BIT = 0o002


def apply_mode(path: Path, mode: int) -> bool:
    """Try to set `mode` on `path`. Returns whether a mode is now in force."""
    try:
        os.chmod(path, mode)
    except (OSError, NotImplementedError, ValueError):  # pragma: no cover - platform dependent
        return False
    return file_mode(path) == mode


def file_mode(path: Path) -> int | None:
    """The Unix permission bits of `path`, or `None` when the platform does not report modes."""
    try:
        status = os.stat(path, follow_symlinks=False)
    except (OSError, NotImplementedError, ValueError):  # pragma: no cover - platform dependent
        return None
    mode = stat.S_IMODE(status.st_mode)
    return mode if isinstance(mode, int) else None


def is_world_writable(path: Path) -> bool:
    """Whether anyone on the machine may write to `path`."""
    mode = file_mode(path)
    return mode is not None and bool(mode & _WORLD_WRITE_BIT)


def is_owner_only(path: Path) -> bool:
    """Whether a private object currently has no group or other bits at all."""
    mode = file_mode(path)
    return mode is not None and not mode & _GROUP_OR_OTHER_BITS


def _missing_ancestors(path: Path) -> list[Path]:
    """Every directory on the way to `path` that does not exist yet, nearest last."""
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    return list(reversed(missing))


def ensure_private_directory(path: Path) -> Path:
    """Create `path` (and any missing parent) and lock down the directories just created.

    Directories that already exist are left exactly as they are: their modes are the user's
    business, and the integrity check reports the unsafe ones instead of rewriting them.
    """
    target = Path(path)
    created = _missing_ancestors(target)
    target.mkdir(parents=True, exist_ok=True)
    for directory in created:
        apply_mode(directory, PRIVATE_DIRECTORY_MODE)
    return target


class LocalFileModes:
    """The `FileModeInspector` port over `os.stat`. Read-only by construction."""

    def inspect(self, path: Path) -> ObjectMode:
        """Report the permission bits of `path`, or say that this platform does not."""
        return ObjectMode(
            mode=file_mode(path),
            world_writable=is_world_writable(path),
            owner_only=is_owner_only(path),
        )


def ensure_private_file(path: Path) -> Path:
    """Tighten a file the project just created to `0600`, if it exists."""
    target = Path(path)
    if target.exists():
        apply_mode(target, PRIVATE_FILE_MODE)
    return target


__all__ = [
    "PRIVATE_DIRECTORY_MODE",
    "PRIVATE_FILE_MODE",
    "LocalFileModes",
    "apply_mode",
    "ensure_private_directory",
    "ensure_private_file",
    "file_mode",
    "is_owner_only",
    "is_world_writable",
]
