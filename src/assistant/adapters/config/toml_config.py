"""Reading `config.toml` into the domain configuration (ADR-0013).

The default location is `$XDG_CONFIG_HOME/growing-assistant/config.toml`
(`~/.config/growing-assistant/config.toml`). A missing file is not an error: it means "no
roots configured yet", and the daemon still runs.

Path handling happens here, because the domain is filesystem-free: `~` is expanded and every
configured path must already be absolute. `resolve()` is deliberately **not** used — the
configured path must not silently become a different one through a symlink.
"""

from __future__ import annotations

import asyncio
import os
import tomllib
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from assistant.adapters.config.mail_overlay import merge_accounts, overlay_path, read_overlay
from assistant.domain.config import AssistantConfig
from assistant.domain.errors import InvalidAssistantConfig

APP_DIRECTORY = "growing-assistant"
CONFIG_FILENAME = "config.toml"


def default_config_path() -> Path:
    """Where the host configuration lives unless a caller says otherwise."""
    override = os.environ.get("XDG_CONFIG_HOME")
    base = Path(override).expanduser() if override else Path.home() / ".config"
    return base / APP_DIRECTORY / CONFIG_FILENAME


class TomlConfigLoader:
    """`ConfigLoader` backed by a TOML file on the host, plus the managed mail overlay (ADR-0043).

    The user's file is read exactly as before and is never rewritten. When a managed
    `mail-accounts.toml` exists beside it, its accounts are merged by id (the overlay wins), which
    is the only thing the settings page can change without restarting anything. A host that has no
    overlay behaves identically to v1.2, which is what makes this backward compatible rather than
    a migration.
    """

    def __init__(self, path: Path | None = None, *, managed_overlay: bool = True) -> None:
        self._path = Path(path) if path is not None else default_config_path()
        self._managed_overlay = managed_overlay

    @property
    def path(self) -> Path:
        """The file this loader reads."""
        return self._path

    async def load(self) -> AssistantConfig:
        return await asyncio.to_thread(self._load_sync)

    def _load_sync(self) -> AssistantConfig:
        if not self._path.is_file():
            return AssistantConfig.empty()
        try:
            with self._path.open("rb") as handle:
                data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise InvalidAssistantConfig(
                f"{self._path} is not valid TOML: {exc}"
            ) from exc
        except OSError as exc:
            raise InvalidAssistantConfig(f"{self._path} could not be read: {exc}") from exc
        config = AssistantConfig.from_mapping(_expand_root_paths(data))
        if not self._managed_overlay:
            return config
        managed = read_overlay(overlay_path(self._path))
        if not managed:
            return config
        merged = merge_accounts(config.mail.accounts, managed)
        return replace(config, mail=replace(config.mail, accounts=merged))


def _expand_root_paths(data: Mapping[str, object]) -> Mapping[str, object]:
    """Expand `~` in configured paths, leaving everything else untouched."""
    storage = data.get("storage")
    if not isinstance(storage, Mapping):
        return data
    entries = storage.get("roots")
    if not isinstance(entries, list):
        return data
    expanded_entries: list[object] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            expanded_entries.append(entry)
            continue
        path = entry.get("path")
        if isinstance(path, str):
            expanded_entries.append({**entry, "path": str(Path(path).expanduser())})
        else:
            expanded_entries.append(entry)
    return {**data, "storage": {**storage, "roots": expanded_entries}}


__all__ = ["APP_DIRECTORY", "CONFIG_FILENAME", "TomlConfigLoader", "default_config_path"]
