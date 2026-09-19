"""Host configuration: which storage roots this host manages (ADR-0013).

Configuration is *explicit authorisation*, not discovery: the assistant never guesses that
`/mnt/e` or a directory in `$HOME` is user data. A configured physical path is a host-local
runtime location; the stable identity is still `root_id` (plus a vault's manifest).

Vault labels come from `.pa/vault.toml`, so a label written in the config is accepted but
never treated as authority.

Paths are validated as POSIX-absolute strings here; `~` expansion happens in the config
adapter, which keeps this module free of filesystem access.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from assistant.domain.errors import InvalidAssistantConfig, InvalidStorageRoot
from assistant.domain.storage import StorageKind, validate_root_id

CONFIG_FORMAT_VERSION: Final[int] = 1
DEFAULT_INDEX_INTERVAL_SECONDS: Final[int] = 300
MIN_INDEX_INTERVAL_SECONDS: Final[int] = 10
MAX_INDEX_INTERVAL_SECONDS: Final[int] = 86400

_KNOWN_TOP_LEVEL_KEYS = frozenset({"format_version", "indexing", "storage"})
_KNOWN_INDEXING_KEYS = frozenset({"interval_seconds", "run_on_startup"})
_KNOWN_ROOT_KEYS = frozenset({"kind", "id", "label", "path", "enabled"})


@dataclass(frozen=True, slots=True)
class IndexingConfig:
    """How often the daemon reconciles storage roots."""

    interval_seconds: int = DEFAULT_INDEX_INTERVAL_SECONDS
    run_on_startup: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.interval_seconds, int) or isinstance(self.interval_seconds, bool):
            raise InvalidAssistantConfig("indexing.interval_seconds must be an integer")
        if not MIN_INDEX_INTERVAL_SECONDS <= self.interval_seconds <= MAX_INDEX_INTERVAL_SECONDS:
            raise InvalidAssistantConfig(
                "indexing.interval_seconds must be between "
                f"{MIN_INDEX_INTERVAL_SECONDS} and {MAX_INDEX_INTERVAL_SECONDS}"
            )
        if not isinstance(self.run_on_startup, bool):
            raise InvalidAssistantConfig("indexing.run_on_startup must be a boolean")


@dataclass(frozen=True, slots=True)
class ConfiguredStorageRoot:
    """One explicitly authorised storage root."""

    root_id: str
    kind: StorageKind
    path: str
    label: str = ""
    enabled: bool = True

    def __post_init__(self) -> None:
        try:
            validate_root_id(self.root_id)
        except InvalidStorageRoot as exc:
            raise InvalidAssistantConfig(str(exc)) from exc
        _validate_absolute_path(self.path)


@dataclass(frozen=True, slots=True)
class ConfiguredLocalRoot(ConfiguredStorageRoot):
    """A local folder; the caller supplies its label."""

    kind: StorageKind = field(default=StorageKind.LOCAL, init=False)

    def __post_init__(self) -> None:
        # Called explicitly: with `slots=True` the dataclass decorator rebuilds the class, and
        # a zero-argument `super()` in the original body would bind to the wrong class.
        ConfiguredStorageRoot.__post_init__(self)
        if not self.label.strip():
            raise InvalidAssistantConfig("a local root needs a non-blank label")


@dataclass(frozen=True, slots=True)
class ConfiguredVaultRoot(ConfiguredStorageRoot):
    """An archive vault; its label comes from the manifest, never from the config."""

    kind: StorageKind = field(default=StorageKind.VAULT, init=False)


@dataclass(frozen=True, slots=True)
class AssistantConfig:
    """The whole host configuration."""

    roots: tuple[ConfiguredStorageRoot, ...] = ()
    indexing: IndexingConfig = field(default_factory=IndexingConfig)
    format_version: int = CONFIG_FORMAT_VERSION

    def __post_init__(self) -> None:
        if self.format_version != CONFIG_FORMAT_VERSION:
            raise InvalidAssistantConfig(
                f"unsupported config format_version {self.format_version}; "
                f"this build understands {CONFIG_FORMAT_VERSION}"
            )
        seen: set[str] = set()
        for root in self.roots:
            if root.root_id in seen:
                raise InvalidAssistantConfig(f"duplicate storage root id {root.root_id!r}")
            seen.add(root.root_id)

    @classmethod
    def empty(cls) -> AssistantConfig:
        """A configuration with no roots: the daemon runs, it just has nothing to watch."""
        return cls()

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> AssistantConfig:
        """Parse a config document that has already been read from TOML."""
        unknown = sorted(set(data) - _KNOWN_TOP_LEVEL_KEYS)
        if unknown:
            raise InvalidAssistantConfig(f"unknown config keys: {', '.join(unknown)}")
        format_version = data.get("format_version")
        if not isinstance(format_version, int) or isinstance(format_version, bool):
            raise InvalidAssistantConfig("format_version must be an integer")
        indexing = _parse_indexing(data.get("indexing"))
        roots = _parse_roots(data.get("storage"))
        return cls(roots=roots, indexing=indexing, format_version=format_version)

    @property
    def enabled_roots(self) -> tuple[ConfiguredStorageRoot, ...]:
        """Roots the user wants reconciled."""
        return tuple(root for root in self.roots if root.enabled)

    def find(self, root_id: str) -> ConfiguredStorageRoot | None:
        """Return the configured root with that id, or `None`."""
        for root in self.roots:
            if root.root_id == root_id:
                return root
        return None


def _validate_absolute_path(path: str) -> None:
    if not path.strip():
        raise InvalidAssistantConfig("a configured root needs a non-blank path")
    if not path.startswith("/"):
        raise InvalidAssistantConfig(f"configured root path must be absolute: {path!r}")
    if "\x00" in path:
        raise InvalidAssistantConfig("configured root path must not contain NUL bytes")


def _parse_indexing(value: object) -> IndexingConfig:
    if value is None:
        return IndexingConfig()
    if not isinstance(value, Mapping):
        raise InvalidAssistantConfig("[indexing] must be a table")
    unknown = sorted(set(value) - _KNOWN_INDEXING_KEYS)
    if unknown:
        raise InvalidAssistantConfig(f"unknown [indexing] keys: {', '.join(unknown)}")
    interval = value.get("interval_seconds", DEFAULT_INDEX_INTERVAL_SECONDS)
    run_on_startup = value.get("run_on_startup", True)
    if not isinstance(interval, int) or isinstance(interval, bool):
        raise InvalidAssistantConfig("indexing.interval_seconds must be an integer")
    if not isinstance(run_on_startup, bool):
        raise InvalidAssistantConfig("indexing.run_on_startup must be a boolean")
    return IndexingConfig(interval_seconds=interval, run_on_startup=run_on_startup)


def _parse_roots(value: object) -> tuple[ConfiguredStorageRoot, ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise InvalidAssistantConfig("[storage] must be a table")
    unknown = sorted(set(value) - {"roots"})
    if unknown:
        raise InvalidAssistantConfig(f"unknown [storage] keys: {', '.join(unknown)}")
    entries = value.get("roots", [])
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise InvalidAssistantConfig("[[storage.roots]] must be a list of tables")
    roots: list[ConfiguredStorageRoot] = []
    for entry in entries:
        roots.append(_parse_root(entry))
    return tuple(roots)


def _parse_root(entry: object) -> ConfiguredStorageRoot:
    if not isinstance(entry, Mapping):
        raise InvalidAssistantConfig("each [[storage.roots]] entry must be a table")
    unknown = sorted(set(entry) - _KNOWN_ROOT_KEYS)
    if unknown:
        raise InvalidAssistantConfig(
            f"unknown storage root keys: {', '.join(unknown)}"
        )
    kind_value = entry.get("kind")
    if not isinstance(kind_value, str):
        raise InvalidAssistantConfig("a storage root needs a string kind")
    try:
        kind = StorageKind(kind_value)
    except ValueError as exc:
        raise InvalidAssistantConfig(f"unknown storage root kind {kind_value!r}") from exc
    root_id = entry.get("id")
    if not isinstance(root_id, str):
        raise InvalidAssistantConfig("a storage root needs a string id")
    path = entry.get("path")
    if not isinstance(path, str):
        raise InvalidAssistantConfig("a storage root needs a string path")
    enabled = entry.get("enabled", True)
    if not isinstance(enabled, bool):
        raise InvalidAssistantConfig("storage root enabled must be a boolean")
    label = entry.get("label", "")
    if not isinstance(label, str):
        raise InvalidAssistantConfig("storage root label must be a string")
    if kind is StorageKind.LOCAL:
        return ConfiguredLocalRoot(
            root_id=root_id, path=path, label=label, enabled=enabled
        )
    return ConfiguredVaultRoot(root_id=root_id, path=path, label="", enabled=enabled)


__all__ = [
    "CONFIG_FORMAT_VERSION",
    "DEFAULT_INDEX_INTERVAL_SECONDS",
    "MAX_INDEX_INTERVAL_SECONDS",
    "MIN_INDEX_INTERVAL_SECONDS",
    "AssistantConfig",
    "ConfiguredLocalRoot",
    "ConfiguredStorageRoot",
    "ConfiguredVaultRoot",
    "IndexingConfig",
]
