"""Integration tests for periodic reconciliation of configured roots (ADR-0013)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from assistant.adapters.content.registry import SuffixExtractorRegistry
from assistant.adapters.content.text import TextContentExtractor
from assistant.adapters.filesystem.scanner import FilesystemScanner
from assistant.adapters.filesystem.vault_manifest import VaultManifestFile
from assistant.adapters.interval_waiter import AsyncioIntervalWaiter
from assistant.adapters.knowledge.index_location import KnowledgeIndexLocator
from assistant.application.index_sync import IndexSyncService, RootSyncStatus
from assistant.application.knowledge_indexer import KnowledgeIndexer
from assistant.application.knowledge_search import KnowledgeSearchService
from assistant.application.storage_catalog import StorageCatalogService
from assistant.domain.catalog import (
    CatalogEntry,
    CatalogRoot,
    CatalogScanResult,
    FilesystemSnapshot,
    ScanError,
)
from assistant.domain.config import (
    AssistantConfig,
    ConfiguredLocalRoot,
    ConfiguredVaultRoot,
)
from assistant.domain.errors import ConfiguredRootNotFound
from assistant.domain.storage import StorageKind, StorageUri
from assistant.store.catalog import SqliteCatalogRepository
from assistant.store.db import Database
from assistant.store.errors import StoreError
from assistant.store.knowledge_index import SqliteKnowledgeIndexFactory
from assistant.store.migrations import apply_migrations
from tests.support.fakes import FakeClock

START = datetime(2026, 9, 20, 16, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=START)


@dataclass
class Wiring:
    service: IndexSyncService
    catalog: SqliteCatalogRepository
    database: Database
    cache_root: Path
    indexer: KnowledgeIndexer
    indexes: SqliteKnowledgeIndexFactory
    manifests: VaultManifestFile
    scanner: object

    def search_service(self) -> KnowledgeSearchService:
        return KnowledgeSearchService(
            self.catalog, self.indexes, self.manifests
        )


def _wiring(
    tmp_path: Path,
    clock: FakeClock,
    config: AssistantConfig,
    *,
    scanner: object | None = None,
    catalog_repository: object | None = None,
) -> Wiring:
    database = Database.at(tmp_path / "host" / "assistant.db")
    apply_migrations(database, clock=clock)
    catalog = SqliteCatalogRepository(database)
    manifests = VaultManifestFile(clock)
    chosen_scanner = scanner if scanner is not None else FilesystemScanner(clock)
    catalog_service = StorageCatalogService(
        chosen_scanner,  # type: ignore[arg-type]
        manifests,
        catalog_repository if catalog_repository is not None else catalog,  # type: ignore[arg-type]
        clock,
    )
    cache_root = tmp_path / "cache" / "knowledge"
    indexes = SqliteKnowledgeIndexFactory(KnowledgeIndexLocator(cache_root=cache_root))
    indexer = KnowledgeIndexer(
        catalog, SuffixExtractorRegistry(), indexes, manifests, clock
    )
    service = IndexSyncService(
        config,
        catalog_service,
        indexer,
        manifests,
        clock,
        AsyncioIntervalWaiter(),
        enable_logging=False,
    )
    return Wiring(
        service=service,
        catalog=catalog,
        database=database,
        cache_root=cache_root,
        indexer=indexer,
        indexes=indexes,
        manifests=manifests,
        scanner=chosen_scanner,
    )


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


async def _make_vault(
    manifests: VaultManifestFile, path: Path, vault_id: str = "archive-main"
) -> None:
    path.mkdir(parents=True, exist_ok=True)
    await manifests.initialize(path, vault_id=vault_id, label="Archive")

async def _search(wiring: Wiring, query: str) -> list[str]:
    result = await wiring.search_service().search(query, limit=10)
    return [str(hit.hit.logical_uri) for hit in result.content_hits]


async def test_local_root_is_synced_indexed_and_searchable(
    tmp_path: Path, clock: FakeClock
) -> None:
    documents = tmp_path / "documents" / "university"
    _write(documents / "notes" / "deadlines.md", "the important deadline is Friday\n")
    config = AssistantConfig(
        roots=(
            ConfiguredLocalRoot(
                root_id="university", path=str(documents), label="University"
            ),
        )
    )
    wiring = _wiring(tmp_path, clock, config)

    result = await wiring.service.sync_once()

    root = result.roots[0]
    assert root.status is RootSyncStatus.SYNCED
    assert root.catalog_result is not None and root.catalog_result.created == 1
    assert root.knowledge_result is not None and root.knowledge_result.indexed == 1
    stored_root = await wiring.catalog.get_root("university")
    assert stored_root is not None
    assert stored_root.last_known_path == str(documents)
    assert (wiring.cache_root / "university" / "index.sqlite3").is_file()
    assert await _search(wiring, "important deadline") == [
        "local://university/notes/deadlines.md"
    ]


async def test_changes_are_picked_up_and_unchanged_files_are_not_reread(
    tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    documents = tmp_path / "documents" / "university"
    _write(documents / "notes.md", "first version about compilers\n")
    config = AssistantConfig(
        roots=(
            ConfiguredLocalRoot(
                root_id="university", path=str(documents), label="University"
            ),
        )
    )
    wiring = _wiring(tmp_path, clock, config)
    await wiring.service.sync_once()

    _write(documents / "notes.md", "second version about eigenvectors\n")
    clock.advance(60)
    changed = await wiring.service.sync_once()

    assert changed.roots[0].status is RootSyncStatus.SYNCED
    assert changed.roots[0].catalog_result is not None
    assert changed.roots[0].catalog_result.updated == 1
    assert await _search(wiring, "compilers") == []
    assert await _search(wiring, "eigenvectors") == [
        "local://university/notes.md"
    ]

    async def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("an unchanged document must not be re-read")

    monkeypatch.setattr(TextContentExtractor, "extract", forbidden)
    clock.advance(60)
    unchanged = await wiring.service.sync_once()

    assert unchanged.roots[0].knowledge_result is not None
    assert unchanged.roots[0].knowledge_result.skipped_unchanged == 1


async def test_vault_sync_writes_the_index_inside_the_vault(
    tmp_path: Path, clock: FakeClock
) -> None:
    vault = tmp_path / "usb" / "archive"
    manifests = VaultManifestFile(clock)
    await _make_vault(manifests, vault)
    _write(vault / "Courses" / "report.md", "course report deadline\n")
    config = AssistantConfig(
        roots=(ConfiguredVaultRoot(root_id="archive-main", path=str(vault)),)
    )
    wiring = _wiring(tmp_path, clock, config)

    result = await wiring.service.sync_once()

    assert result.roots[0].status is RootSyncStatus.SYNCED
    assert (vault / ".pa" / "index.sqlite3").is_file()
    assert await _search(wiring, "course report") == [
        "vault://archive-main/Courses/report.md"
    ]


async def test_offline_vault_then_back_online(tmp_path: Path, clock: FakeClock) -> None:
    vault = tmp_path / "usb" / "archive"
    manifests = VaultManifestFile(clock)
    await _make_vault(manifests, vault)
    _write(vault / "notes.md", "vault content that stays put\n")
    config = AssistantConfig(
        roots=(ConfiguredVaultRoot(root_id="archive-main", path=str(vault)),)
    )
    wiring = _wiring(tmp_path, clock, config)
    await wiring.service.sync_once()
    before = await wiring.catalog.list_by_root("archive-main")

    unplugged = tmp_path / "usb-unplugged"
    vault.rename(unplugged)
    offline = await wiring.service.sync_once()

    assert offline.roots[0].status is RootSyncStatus.OFFLINE
    assert offline.roots[0].catalog_result is None
    after = await wiring.catalog.list_by_root("archive-main")
    assert [entry.id for entry in after] == [entry.id for entry in before]
    assert all(entry.is_present for entry in after)

    unplugged.rename(vault)
    clock.advance(60)
    recovered = await wiring.service.sync_once()

    assert recovered.roots[0].status is RootSyncStatus.SYNCED


async def test_wrong_vault_at_the_configured_path_is_rejected(
    tmp_path: Path, clock: FakeClock
) -> None:
    vault = tmp_path / "usb" / "archive"
    manifests = VaultManifestFile(clock)
    await _make_vault(manifests, vault, vault_id="archive-other")
    _write(vault / "secret.md", "content of another vault\n")
    config = AssistantConfig(
        roots=(ConfiguredVaultRoot(root_id="archive-main", path=str(vault)),)
    )
    wiring = _wiring(tmp_path, clock, config)

    result = await wiring.service.sync_once()

    assert result.roots[0].status is RootSyncStatus.IDENTITY_MISMATCH
    assert await wiring.catalog.get_root("archive-main") is None
    assert not (vault / ".pa" / "index.sqlite3").exists()
    assert list(wiring.cache_root.rglob("index.sqlite3")) == []


async def test_incomplete_scan_skips_indexing_and_keeps_old_content(
    tmp_path: Path, clock: FakeClock
) -> None:
    vault = tmp_path / "usb" / "archive"
    manifests = VaultManifestFile(clock)
    await _make_vault(manifests, vault)
    _write(vault / "a.md", "searchable content alpha\n")
    _write(vault / "locked" / "b.md", "searchable content beta\n")
    config = AssistantConfig(
        roots=(ConfiguredVaultRoot(root_id="archive-main", path=str(vault)),)
    )
    wiring = _wiring(tmp_path, clock, config)
    await wiring.service.sync_once()
    assert await _search(wiring, "content beta") == [
        "vault://archive-main/locked/b.md"
    ]

    partial = FilesystemSnapshot(
        entries=(
            _snapshot_entry("a.md", size="searchable content alpha"),
        ),
        complete=False,
        errors=(ScanError(relative_path="locked", error_type="PermissionError", message="denied"),),
        skipped_symlinks=(),
        started_at=START,
        finished_at=START,
    )
    broken = _wiring(
        tmp_path, clock, config, scanner=PartialScanner(partial)
    )
    clock.advance(60)
    incomplete = await broken.service.sync_once()

    assert incomplete.roots[0].status is RootSyncStatus.INCOMPLETE
    assert incomplete.roots[0].knowledge_result is None
    # Old content is untouched: no reindex ran, so nothing was dropped.
    assert await _search(broken, "content beta") == [
        "vault://archive-main/locked/b.md"
    ]
    entries = await broken.catalog.list_by_root("archive-main")
    assert {entry.relative_path for entry in entries} == {"a.md", "locked/b.md"}


async def test_multi_root_isolation(tmp_path: Path, clock: FakeClock) -> None:
    good = tmp_path / "documents" / "good"
    _write(good / "notes.md", "good root content\n")
    good_two = tmp_path / "documents" / "good-two"
    _write(good_two / "notes.md", "second good root content\n")
    missing_mount = tmp_path / "usb-offline" / "archive"
    wrong_vault = tmp_path / "usb-wrong" / "archive"
    manifests = VaultManifestFile(clock)
    await _make_vault(manifests, wrong_vault, vault_id="someone-else")
    config = AssistantConfig(
        roots=(
            ConfiguredLocalRoot(root_id="local-good", path=str(good), label="Good"),
            ConfiguredVaultRoot(root_id="vault-offline", path=str(missing_mount)),
            ConfiguredVaultRoot(root_id="vault-wrong", path=str(wrong_vault)),
            ConfiguredLocalRoot(
                root_id="local-good-2", path=str(good_two), label="Good two"
            ),
        )
    )
    wiring = _wiring(tmp_path, clock, config)

    result = await wiring.service.sync_once()

    assert [root.status for root in result.roots] == [
        RootSyncStatus.SYNCED,
        RootSyncStatus.OFFLINE,
        RootSyncStatus.IDENTITY_MISMATCH,
        RootSyncStatus.SYNCED,
    ]
    assert (result.configured, result.synced, result.offline, result.failed) == (4, 2, 1, 1)
    assert await wiring.catalog.get_root("local-good-2") is not None


async def test_disabled_roots_are_reported_but_not_synced(
    tmp_path: Path, clock: FakeClock
) -> None:
    documents = tmp_path / "documents" / "university"
    _write(documents / "notes.md", "content\n")
    config = AssistantConfig(
        roots=(
            ConfiguredLocalRoot(
                root_id="university", path=str(documents), label="University", enabled=False
            ),
        )
    )
    wiring = _wiring(tmp_path, clock, config)

    result = await wiring.service.sync_once()

    assert result.roots[0].status is RootSyncStatus.DISABLED
    assert await wiring.catalog.get_root("university") is None


async def test_explicit_root_selection(tmp_path: Path, clock: FakeClock) -> None:
    first = tmp_path / "documents" / "first"
    second = tmp_path / "documents" / "second"
    _write(first / "a.md", "first content\n")
    _write(second / "b.md", "second content\n")
    config = AssistantConfig(
        roots=(
            ConfiguredLocalRoot(root_id="first", path=str(first), label="First"),
            ConfiguredLocalRoot(root_id="second", path=str(second), label="Second"),
        )
    )
    wiring = _wiring(tmp_path, clock, config)

    result = await wiring.service.sync_once("second")

    assert [root.root_id for root in result.roots] == ["second"]
    assert await wiring.catalog.get_root("first") is None
    with pytest.raises(ConfiguredRootNotFound):
        await wiring.service.sync_once("unknown-root")


async def test_root_level_symlink_is_refused(tmp_path: Path, clock: FakeClock) -> None:
    real = tmp_path / "documents" / "real"
    _write(real / "notes.md", "content\n")
    link = tmp_path / "documents" / "link"
    link.symlink_to(real, target_is_directory=True)
    config = AssistantConfig(
        roots=(ConfiguredLocalRoot(root_id="university", path=str(link), label="Linked"),)
    )
    wiring = _wiring(tmp_path, clock, config)

    result = await wiring.service.sync_once()

    assert result.roots[0].status is RootSyncStatus.SCAN_ERROR
    assert "symlink" in (result.roots[0].error or "")
    assert await wiring.catalog.get_root("university") is None


async def test_root_that_is_a_file_is_a_scan_error(
    tmp_path: Path, clock: FakeClock
) -> None:
    target = _write(tmp_path / "documents" / "notes.md", "content\n")
    config = AssistantConfig(
        roots=(ConfiguredLocalRoot(root_id="university", path=str(target), label="File"),)
    )
    wiring = _wiring(tmp_path, clock, config)

    result = await wiring.service.sync_once()

    assert result.roots[0].status is RootSyncStatus.SCAN_ERROR


async def test_catalog_infrastructure_failures_propagate(
    tmp_path: Path, clock: FakeClock
) -> None:
    documents = tmp_path / "documents" / "university"
    _write(documents / "notes.md", "content\n")
    config = AssistantConfig(
        roots=(ConfiguredLocalRoot(root_id="university", path=str(documents), label="U"),)
    )
    wiring = _wiring(tmp_path, clock, config, catalog_repository=BrokenCatalogRepository())

    with pytest.raises(StoreError):
        await wiring.service.sync_once()


async def test_concurrent_sync_calls_are_serialised(
    tmp_path: Path, clock: FakeClock
) -> None:
    documents = tmp_path / "documents" / "university"
    _write(documents / "notes.md", "content\n")
    config = AssistantConfig(
        roots=(ConfiguredLocalRoot(root_id="university", path=str(documents), label="U"),)
    )
    gate = asyncio.Event()
    scanner = GatedScanner(FilesystemScanner(clock), gate)
    wiring = _wiring(tmp_path, clock, config, scanner=scanner)

    first = asyncio.create_task(wiring.service.sync_once())
    await _wait_until(lambda: scanner.entered == 1)
    second = asyncio.create_task(wiring.service.sync_once())
    await asyncio.sleep(0.05)
    gate.set()
    results = await asyncio.gather(first, second)

    assert scanner.max_concurrent == 1
    assert [result.roots[0].status for result in results] == [
        RootSyncStatus.SYNCED,
        RootSyncStatus.SYNCED,
    ]


async def test_search_during_replacement_never_sees_a_half_document(
    tmp_path: Path, clock: FakeClock
) -> None:
    documents = tmp_path / "documents" / "university"
    old_text = "\n".join(f"alpha line {number}" for number in range(1, 400))
    new_text = "\n".join(f"beta line {number}" for number in range(1, 400))
    _write(documents / "notes.md", old_text)
    config = AssistantConfig(
        roots=(ConfiguredLocalRoot(root_id="university", path=str(documents), label="U"),)
    )
    wiring = _wiring(tmp_path, clock, config)
    await wiring.service.sync_once()
    search = wiring.search_service()

    async def observed() -> set[str]:
        seen: set[str] = set()
        for _ in range(30):
            for query, label in (("alpha line", "alpha"), ("beta line", "beta")):
                result = await search.search(query, limit=5)
                if result.content_hits:
                    seen.add(label)
        return seen

    _write(documents / "notes.md", new_text)
    clock.advance(60)
    _, seen = await asyncio.gather(
        wiring.service.sync_once(force_index=True), observed()
    )

    assert seen <= {"alpha", "beta"}
    final = await search.search("beta line", limit=5)
    assert final.content_hits
    assert (await search.search("alpha line", limit=5)).content_hits == ()


class PartialScanner:
    """Returns one fixed (incomplete) snapshot instead of touching the filesystem."""

    def __init__(self, snapshot: FilesystemSnapshot) -> None:
        self._snapshot = snapshot

    async def scan(self, path: Path) -> FilesystemSnapshot:
        return self._snapshot


class GatedScanner:
    """Blocks every scan until the gate opens, recording concurrency."""

    def __init__(self, inner: FilesystemScanner, gate: asyncio.Event) -> None:
        self._inner = inner
        self._gate = gate
        self.entered = 0
        self.max_concurrent = 0

    async def scan(self, path: Path) -> FilesystemSnapshot:
        self.entered += 1
        self.max_concurrent = max(self.max_concurrent, self.entered)
        try:
            await self._gate.wait()
            return await self._inner.scan(path)
        finally:
            self.entered -= 1


class BrokenCatalogRepository:
    """Catalog store whose writes fail with a host-database error."""

    async def apply_snapshot(self, **_kwargs: object) -> CatalogScanResult:
        raise StoreError("host catalog is unavailable")

    async def get_root(self, root_id: str) -> CatalogRoot | None:
        return None

    async def list_roots(self) -> list[CatalogRoot]:
        return []

    async def list_by_root(
        self, root_id: str, *, include_missing: bool = False, limit: int | None = None
    ) -> list[CatalogEntry]:
        return []

    async def get_by_uri(self, uri: StorageUri) -> CatalogEntry | None:
        return None

    async def get_by_location(
        self, *, storage_kind: StorageKind, root_id: str, relative_path: str
    ) -> CatalogEntry | None:
        return None

    async def search_metadata(
        self,
        query: str,
        *,
        limit: int,
        root_id: str | None = None,
        include_missing: bool = False,
    ) -> list[CatalogEntry]:
        return []


def _snapshot_entry(relative_path: str, *, size: str) -> object:
    from assistant.domain.catalog import FileSnapshotEntry

    return FileSnapshotEntry(
        relative_path=relative_path,
        name=relative_path.rsplit("/", 1)[-1],
        size_bytes=len(size),
        mtime_ns=1_000_000,
        media_type="text/markdown",
    )


async def _wait_until(predicate: object, *, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():  # type: ignore[operator]
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met before the timeout")
