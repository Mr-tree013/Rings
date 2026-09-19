"""Unit tests for per-root index locations (ADR-0012)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from assistant.adapters.knowledge.index_location import (
    KnowledgeIndexLocator,
    default_local_cache_root,
)
from assistant.domain.catalog import CatalogRoot
from assistant.domain.storage import StorageKind, StorageRoot

NOW = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)


def _root(kind: StorageKind, root_id: str, path: str) -> CatalogRoot:
    return CatalogRoot(
        root=StorageRoot(root_id=root_id, kind=kind, label="Root"),
        last_known_path=path,
        first_seen_at=NOW,
        last_seen_at=NOW,
        last_scanned_at=NOW,
    )


def test_vault_index_lives_inside_the_vault(tmp_path: Path) -> None:
    locator = KnowledgeIndexLocator(cache_root=tmp_path / "cache")

    location = locator.location_for(
        _root(StorageKind.VAULT, "archive-main", str(tmp_path / "usb"))
    )

    assert location == tmp_path / "usb" / ".pa" / "index.sqlite3"


def test_local_index_lives_in_the_cache_not_the_source(tmp_path: Path) -> None:
    locator = KnowledgeIndexLocator(cache_root=tmp_path / "cache")

    location = locator.location_for(
        _root(StorageKind.LOCAL, "university", str(tmp_path / "documents"))
    )

    assert location == tmp_path / "cache" / "university" / "index.sqlite3"
    assert tmp_path / "documents" not in location.parents


def test_default_cache_root_follows_xdg_cache_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))

    assert default_local_cache_root() == tmp_path / "xdg-cache" / "growing-assistant" / "knowledge"

