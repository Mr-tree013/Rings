"""Filesystem boundaries for runtime state, configuration and cache.

The repository never stores runtime state or personal data; see ADR-0004 and the
"数据目录边界" section of README.md. Phase 0 only *resolves* these locations, it never
creates them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_DIR_NAME = "growing-assistant"


def _xdg_dir(env_var: str, fallback: Path) -> Path:
    """Resolve an XDG-style directory, always scoped to this application."""
    override = os.environ.get(env_var)
    base = Path(override).expanduser() if override else fallback
    return base / APP_DIR_NAME


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Resolved locations for this application's non-repository data."""

    runtime: Path
    config: Path
    cache: Path

    @classmethod
    def resolve(cls) -> AppPaths:
        """Resolve paths from the environment without touching the filesystem."""
        home = Path.home()
        return cls(
            runtime=_xdg_dir("XDG_DATA_HOME", home / ".local" / "share"),
            config=_xdg_dir("XDG_CONFIG_HOME", home / ".config"),
            cache=_xdg_dir("XDG_CACHE_HOME", home / ".cache"),
        )

    def items(self) -> tuple[tuple[str, Path], ...]:
        """Return labelled paths for display and diagnostics."""
        return (
            ("runtime", self.runtime),
            ("config", self.config),
            ("cache", self.cache),
        )

