"""Resolving per-root knowledge index paths (ADR-0012).

```text
vault://archive-main  ->  <vault root>/.pa/index.sqlite3   (travels with the drive)
local://university    ->  $XDG_CACHE_HOME/growing-assistant/knowledge/university/index.sqlite3
```

A vault's index lives inside the vault, never on the host: archive content is not copied into
host runtime storage. Local roots keep their index in the cache directory, never inside the
user's folder tree and never inside the repository.
"""

from __future__ import annotations

import os
from pathlib import Path

from assistant.domain.catalog import CatalogRoot
from assistant.domain.storage import StorageKind

INDEX_FILENAME = "index.sqlite3"
VAULT_INTERNAL_DIRECTORY = ".pa"
APP_CACHE_DIRECTORY = "growing-assistant"


def default_local_cache_root() -> Path:
    """Where local-root indexes live unless a caller says otherwise."""
    override = os.environ.get("XDG_CACHE_HOME")
    base = Path(override).expanduser() if override else Path.home() / ".cache"
    return base / APP_CACHE_DIRECTORY / "knowledge"


class KnowledgeIndexLocator:
    """Maps a catalog root to its index database path."""

    def __init__(self, cache_root: Path | None = None) -> None:
        self._cache_root = (
            Path(cache_root) if cache_root is not None else default_local_cache_root()
        )

    @property
    def cache_root(self) -> Path:
        """The cache directory used for local-root indexes."""
        return self._cache_root

    def location_for(self, root: CatalogRoot) -> Path:
        if root.root.kind is StorageKind.VAULT:
            return (
                Path(root.last_known_path)
                / VAULT_INTERNAL_DIRECTORY
                / INDEX_FILENAME
            )
        return self._cache_root / root.root.root_id / INDEX_FILENAME


__all__ = [
    "APP_CACHE_DIRECTORY",
    "INDEX_FILENAME",
    "VAULT_INTERNAL_DIRECTORY",
    "KnowledgeIndexLocator",
    "default_local_cache_root",
]

