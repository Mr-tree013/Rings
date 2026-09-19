"""Integration tests for incremental knowledge indexing over real files (ADR-0012)."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from assistant.adapters.content.pdf import PdfContentExtractor
from assistant.adapters.content.registry import SuffixExtractorRegistry
from assistant.adapters.content.text import TextContentExtractor
from assistant.adapters.filesystem.scanner import FilesystemScanner
from assistant.adapters.filesystem.vault_manifest import VaultManifestFile
from assistant.adapters.knowledge.index_location import KnowledgeIndexLocator
from assistant.application.knowledge_indexer import KnowledgeIndexer
from assistant.application.storage_catalog import StorageCatalogService
from assistant.domain.errors import (
    StorageRootIdentityMismatch,
    StorageRootOffline,
    UnknownCatalogEntry,
)
from assistant.domain.knowledge import ExtractorInfo, KnowledgeIndexStatus
from assistant.store.catalog import SqliteCatalogRepository
from assistant.store.db import Database
from assistant.store.knowledge_index import SqliteKnowledgeIndexFactory
from assistant.store.migrations import apply_migrations
from tests.support.fakes import FakeClock
from tests.support.pdf_fixtures import build_text_pdf, malformed_pdf

START = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


@dataclass
class Wiring:
    catalog: SqliteCatalogRepository
    indexer: KnowledgeIndexer
    indexes: SqliteKnowledgeIndexFactory
    scanner: FilesystemScanner
    manifests: VaultManifestFile
    cache_root: Path


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=START)


@pytest.fixture
def database(tmp_path: Path, clock: FakeClock) -> Database:
    db = Database.at(tmp_path / "host" / "assistant.db")
    apply_migrations(db, clock=clock)
    return db


@pytest.fixture
def wiring(tmp_path: Path, database: Database, clock: FakeClock) -> Wiring:
    catalog = SqliteCatalogRepository(database)
    manifests = VaultManifestFile(clock)
    cache_root = tmp_path / "cache" / "knowledge"
    indexes = SqliteKnowledgeIndexFactory(KnowledgeIndexLocator(cache_root=cache_root))
    return Wiring(
        catalog=catalog,
        indexer=KnowledgeIndexer(
            catalog, SuffixExtractorRegistry(), indexes, manifests, clock
        ),
        indexes=indexes,
        scanner=FilesystemScanner(clock),
        manifests=manifests,
        cache_root=cache_root,
    )


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


async def _prepare_vault(wiring: Wiring, clock: FakeClock, vault: Path, *files: str) -> None:
    vault.mkdir(parents=True, exist_ok=True)
    await wiring.manifests.initialize(vault, vault_id="archive-main", label="Archive")
    for relative in files:
        _write(vault / relative, f"content of {relative}\n")
    await StorageCatalogService(wiring.scanner, wiring.manifests, wiring.catalog, clock).scan_vault(
        vault
    )


def _vault_index_path(vault: Path) -> Path:
    return vault / ".pa" / "index.sqlite3"


async def test_indexes_text_files_and_searches_them(
    wiring: Wiring, clock: FakeClock, tmp_path: Path
) -> None:
    vault = tmp_path / "usb" / "archive"
    await _prepare_vault(wiring, clock, vault, "notes/deadlines.md")
    _write(vault / "notes" / "deadlines.md", "the important deadline is Friday\n")
    await StorageCatalogService(wiring.scanner, wiring.manifests, wiring.catalog, clock).scan_vault(
        vault
    )

    result = await wiring.indexer.index_root("archive-main")

    assert (result.seen, result.indexed, result.errors) == (1, 1, 0)
    assert _vault_index_path(vault).is_file()
    index = await wiring.indexes.open(await _root(wiring), create=False)
    assert index is not None
    hits = await index.search("important deadline", limit=5)
    assert len(hits) == 1
    assert str(hits[0].logical_uri) == "vault://archive-main/notes/deadlines.md"
    state = await index.get_document_state(hits[0].entry_id)
    assert state is not None
    assert state.status is KnowledgeIndexStatus.INDEXED
    assert state.content_sha256 is not None


async def _root(wiring: Wiring):
    root = await wiring.catalog.get_root("archive-main")
    assert root is not None
    return root


async def test_unchanged_files_are_skipped_without_reading_them(
    wiring: Wiring, clock: FakeClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "usb" / "archive"
    await _prepare_vault(wiring, clock, vault, "a.md")
    await wiring.indexer.index_root("archive-main")

    async def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("an unchanged document must not be re-read")

    monkeypatch.setattr(TextContentExtractor, "extract", forbidden)

    second = await wiring.indexer.index_root("archive-main")

    assert second.skipped_unchanged == 1
    assert second.indexed == 0
    assert second.seen == 1


async def test_force_reindexes(clock: FakeClock, wiring: Wiring, tmp_path: Path) -> None:
    vault = tmp_path / "usb" / "archive"
    await _prepare_vault(wiring, clock, vault, "a.md")
    await wiring.indexer.index_root("archive-main")

    forced = await wiring.indexer.index_root("archive-main", force=True)

    assert forced.indexed == 1
    assert forced.skipped_unchanged == 0


async def test_an_extractor_version_bump_forces_reprocessing(
    wiring: Wiring, clock: FakeClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "usb" / "archive"
    await _prepare_vault(wiring, clock, vault, "a.md")
    await wiring.indexer.index_root("archive-main")

    monkeypatch.setattr(
        TextContentExtractor,
        "info",
        property(lambda _self: ExtractorInfo(name="text", version=2)),
    )
    reprocessed = await wiring.indexer.index_root("archive-main")

    assert reprocessed.indexed == 1
    assert reprocessed.skipped_unchanged == 0


async def test_a_changed_file_without_a_rescan_is_an_error(
    wiring: Wiring, clock: FakeClock, tmp_path: Path
) -> None:
    vault = tmp_path / "usb" / "archive"
    await _prepare_vault(wiring, clock, vault, "a.md")
    await wiring.indexer.index_root("archive-main")
    _write(vault / "a.md", "completely different content that is longer\n")

    result = await wiring.indexer.index_root("archive-main")

    assert result.errors == 1
    assert result.indexed == 0
    index = await wiring.indexes.open(await _root(wiring), create=False)
    assert index is not None
    entries = await index.list_document_states()
    assert [state.status for state in entries] == [KnowledgeIndexStatus.ERROR]
    assert await index.search("content of", limit=5) == []


async def test_rescan_then_reindex_updates_the_content(
    wiring: Wiring, clock: FakeClock, tmp_path: Path
) -> None:
    vault = tmp_path / "usb" / "archive"
    await _prepare_vault(wiring, clock, vault, "a.md")
    await wiring.indexer.index_root("archive-main")
    _write(vault / "a.md", "brand new material about eigenvectors\n")

    await StorageCatalogService(wiring.scanner, wiring.manifests, wiring.catalog, clock).scan_vault(
        vault
    )
    clock.advance(60)
    result = await wiring.indexer.index_root("archive-main")

    index = await wiring.indexes.open(await _root(wiring), create=False)
    assert index is not None
    assert result.errors == 0
    assert len(await index.search("eigenvectors", limit=5)) == 1
    assert await index.search("content of", limit=5) == []


async def test_missing_files_lose_their_indexed_content(
    wiring: Wiring, clock: FakeClock, tmp_path: Path
) -> None:
    vault = tmp_path / "usb" / "archive"
    await _prepare_vault(wiring, clock, vault, "a.md")
    await wiring.indexer.index_root("archive-main")
    (vault / "a.md").unlink()

    await StorageCatalogService(wiring.scanner, wiring.manifests, wiring.catalog, clock).scan_vault(
        vault
    )
    result = await wiring.indexer.index_root("archive-main")

    index = await wiring.indexes.open(await _root(wiring), create=False)
    assert index is not None
    assert result.seen == 0
    assert await index.list_document_states() == []
    assert await index.search("content of", limit=5) == []
    catalog_entry = await wiring.catalog.get_by_location(
        storage_kind=await _kind(wiring), root_id="archive-main", relative_path="a.md"
    )
    assert catalog_entry is not None
    assert not catalog_entry.is_present


async def _kind(wiring: Wiring):
    root = await _root(wiring)
    return root.root.kind


async def test_unsupported_files_are_recorded_without_hashing(
    wiring: Wiring, clock: FakeClock, tmp_path: Path
) -> None:
    vault = tmp_path / "usb" / "archive"
    await _prepare_vault(wiring, clock, vault, "notes.md")
    (vault / "photo.bin").write_bytes(b"\x00\x01\x02binary")

    await StorageCatalogService(wiring.scanner, wiring.manifests, wiring.catalog, clock).scan_vault(
        vault
    )
    result = await wiring.indexer.index_root("archive-main")

    index = await wiring.indexes.open(await _root(wiring), create=False)
    assert index is not None
    states = {state.relative_path: state for state in await index.list_document_states()}
    assert result.unsupported == 1
    assert states["photo.bin"].status is KnowledgeIndexStatus.UNSUPPORTED
    assert states["photo.bin"].content_sha256 is None


async def test_empty_text_files_are_empty_not_indexed(
    wiring: Wiring, clock: FakeClock, tmp_path: Path
) -> None:
    vault = tmp_path / "usb" / "archive"
    await _prepare_vault(wiring, clock, vault, "empty.txt")
    _write(vault / "empty.txt", "")
    await StorageCatalogService(wiring.scanner, wiring.manifests, wiring.catalog, clock).scan_vault(
        vault
    )

    result = await wiring.indexer.index_root("archive-main")

    index = await wiring.indexes.open(await _root(wiring), create=False)
    assert index is not None
    states = await index.list_document_states()
    assert result.empty == 1
    assert states[0].status is KnowledgeIndexStatus.EMPTY
    assert states[0].content_sha256 is not None


async def test_one_broken_file_does_not_stop_the_root(
    wiring: Wiring, clock: FakeClock, tmp_path: Path
) -> None:
    vault = tmp_path / "usb" / "archive"
    vault.mkdir(parents=True, exist_ok=True)
    await wiring.manifests.initialize(vault, vault_id="archive-main", label="Archive")
    _write(vault / "good.md", "searchable good content\n")
    (vault / "broken.txt").write_bytes(b"\xff\xfe not utf8")
    (vault / "bad.pdf").write_bytes(malformed_pdf())
    await StorageCatalogService(wiring.scanner, wiring.manifests, wiring.catalog, clock).scan_vault(
        vault
    )

    result = await wiring.indexer.index_root("archive-main")

    index = await wiring.indexes.open(await _root(wiring), create=False)
    assert index is not None
    assert (result.seen, result.indexed, result.errors) == (3, 1, 2)
    assert len(await index.search("searchable good content", limit=5)) == 1


async def test_pdf_pages_are_indexed_with_page_spans(
    wiring: Wiring, clock: FakeClock, tmp_path: Path
) -> None:
    vault = tmp_path / "usb" / "archive"
    vault.mkdir(parents=True, exist_ok=True)
    await wiring.manifests.initialize(vault, vault_id="archive-main", label="Archive")
    (vault / "report.pdf").write_bytes(
        build_text_pdf(["The important deadline is Friday", "Calculus notes on page two"])
    )
    await StorageCatalogService(wiring.scanner, wiring.manifests, wiring.catalog, clock).scan_vault(
        vault
    )

    result = await wiring.indexer.index_root("archive-main")

    index = await wiring.indexes.open(await _root(wiring), create=False)
    assert index is not None
    hits = await index.search("Calculus notes", limit=5)
    assert result.indexed == 1
    assert hits[0].source_span.page_number == 2


async def test_offline_root_is_refused_and_catalog_is_untouched(
    wiring: Wiring, clock: FakeClock, tmp_path: Path
) -> None:
    vault = tmp_path / "usb" / "archive"
    await _prepare_vault(wiring, clock, vault, "a.md")
    before = await wiring.catalog.list_by_root("archive-main")
    shutil.rmtree(vault)

    with pytest.raises(StorageRootOffline):
        await wiring.indexer.index_root("archive-main")

    after = await wiring.catalog.list_by_root("archive-main")
    assert [entry.id for entry in after] == [entry.id for entry in before]
    assert all(entry.is_present for entry in after)


async def test_a_different_vault_at_the_same_mount_is_refused(
    wiring: Wiring, clock: FakeClock, tmp_path: Path
) -> None:
    vault = tmp_path / "usb" / "archive"
    await _prepare_vault(wiring, clock, vault, "a.md")
    shutil.rmtree(vault)
    vault.mkdir(parents=True)
    await wiring.manifests.initialize(vault, vault_id="someone-else", label="Other vault")
    _write(vault / "other.md", "other content\n")

    with pytest.raises(StorageRootIdentityMismatch, match="someone-else"):
        await wiring.indexer.index_root("archive-main")


async def test_local_roots_keep_their_index_in_the_cache(
    wiring: Wiring, clock: FakeClock, tmp_path: Path
) -> None:
    folder = tmp_path / "documents" / "university"
    _write(folder / "notes.md", "local root content about compilers\n")
    await StorageCatalogService(wiring.scanner, wiring.manifests, wiring.catalog, clock).scan_local(
        root_id="university", label="University", path=folder
    )

    result = await wiring.indexer.index_root("university")

    assert result.indexed == 1
    assert (wiring.cache_root / "university" / "index.sqlite3").is_file()
    assert not (folder / "index.sqlite3").exists()
    assert not (folder / ".pa").exists()


async def test_moving_a_vault_keeps_identity_and_index(
    wiring: Wiring, clock: FakeClock, tmp_path: Path
) -> None:
    first_mount = tmp_path / "usb-a" / "archive"
    await _prepare_vault(wiring, clock, first_mount, "notes.md")
    await wiring.indexer.index_root("archive-main")

    second_mount = tmp_path / "usb-b" / "archive"
    second_mount.parent.mkdir(parents=True)
    shutil.copytree(first_mount, second_mount, symlinks=True)
    await StorageCatalogService(wiring.scanner, wiring.manifests, wiring.catalog, clock).scan_vault(
        second_mount
    )
    clock.advance(60)
    result = await wiring.indexer.index_root("archive-main")

    root = await _root(wiring)
    index = await wiring.indexes.open(root, create=False)
    assert index is not None
    hits = await index.search("content of notes.md", limit=5)
    assert root.last_known_path == str(second_mount)
    assert result.skipped_unchanged == 1
    assert str(hits[0].logical_uri) == "vault://archive-main/notes.md"


async def test_index_entry_targets_a_single_document(
    wiring: Wiring, clock: FakeClock, tmp_path: Path
) -> None:
    vault = tmp_path / "usb" / "archive"
    await _prepare_vault(wiring, clock, vault, "a.md", "b.md")
    listed = await wiring.catalog.list_by_root("archive-main")
    entries = {entry.relative_path: entry for entry in listed}

    state = await wiring.indexer.index_entry("archive-main", entries["a.md"].id)

    index = await wiring.indexes.open(await _root(wiring), create=False)
    assert index is not None
    assert state is not None
    assert state.relative_path == "a.md"
    assert [item.relative_path for item in await index.list_document_states()] == ["a.md"]
    with pytest.raises(UnknownCatalogEntry):
        await wiring.indexer.index_entry("archive-main", uuid4())


async def test_oversized_files_are_errors_not_crashes(
    wiring: Wiring, clock: FakeClock, tmp_path: Path
) -> None:
    vault = tmp_path / "usb" / "archive"
    await _prepare_vault(wiring, clock, vault, "big.md")
    strict_indexer = KnowledgeIndexer(
        wiring.catalog,
        SuffixExtractorRegistry(
            [TextContentExtractor(max_bytes=1), PdfContentExtractor(max_bytes=1)]
        ),
        wiring.indexes,
        wiring.manifests,
        clock,
    )

    result = await strict_indexer.index_root("archive-main")

    index = await wiring.indexes.open(await _root(wiring), create=False)
    assert index is not None
    states = await index.list_document_states()
    assert result.errors == 1
    assert states[0].status is KnowledgeIndexStatus.ERROR
    assert "ContentTooLarge" in (states[0].last_error or "")
