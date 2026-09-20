"""Integration tests for cross-root knowledge search (ADR-0012)."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from assistant.adapters.content.registry import SuffixExtractorRegistry
from assistant.adapters.filesystem.scanner import FilesystemScanner
from assistant.adapters.filesystem.vault_manifest import VaultManifestFile
from assistant.adapters.knowledge.index_location import KnowledgeIndexLocator
from assistant.application.knowledge_indexer import KnowledgeIndexer
from assistant.application.knowledge_search import KnowledgeSearchService
from assistant.application.storage_catalog import StorageCatalogService
from assistant.store.catalog import SqliteCatalogRepository
from assistant.store.db import Database
from assistant.store.knowledge_index import SqliteKnowledgeIndexFactory
from assistant.store.migrations import apply_migrations
from tests.support.fakes import FakeClock

START = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)


@dataclass
class Wiring:
    catalog: SqliteCatalogRepository
    indexer: KnowledgeIndexer
    search: KnowledgeSearchService
    scanner: FilesystemScanner
    manifests: VaultManifestFile
    catalog_service: StorageCatalogService


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=START)


@pytest.fixture
def wiring(tmp_path: Path, clock: FakeClock) -> Wiring:
    database = Database.at(tmp_path / "host" / "assistant.db")
    apply_migrations(database, clock=clock)
    catalog = SqliteCatalogRepository(database)
    manifests = VaultManifestFile(clock)
    indexes = SqliteKnowledgeIndexFactory(
        KnowledgeIndexLocator(cache_root=tmp_path / "cache" / "knowledge")
    )
    return Wiring(
        catalog=catalog,
        indexer=KnowledgeIndexer(
            catalog, SuffixExtractorRegistry(), indexes, manifests, clock
        ),
        search=KnowledgeSearchService(catalog, indexes, manifests),
        scanner=FilesystemScanner(clock),
        manifests=manifests,
        catalog_service=StorageCatalogService(
            FilesystemScanner(clock), manifests, catalog, clock
        ),
    )


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


async def _prepare_two_roots(wiring: Wiring, tmp_path: Path) -> tuple[Path, Path]:
    local = tmp_path / "documents" / "university"
    _write(local / "notes" / "compilers.md", "the shared keyword alpha in the local note\n")
    await wiring.catalog_service.scan_local(
        root_id="university", label="University", path=local
    )
    vault = tmp_path / "usb" / "archive"
    vault.mkdir(parents=True)
    await wiring.manifests.initialize(vault, vault_id="archive-main", label="Archive")
    _write(vault / "courses" / "alpha-notes.md", "alpha also appears in the vault copy\n")
    await wiring.catalog_service.scan_vault(vault)
    await wiring.indexer.index_root("university")
    await wiring.indexer.index_root("archive-main")
    return local, vault


async def test_merges_content_hits_from_both_roots_with_rrf(
    wiring: Wiring, tmp_path: Path
) -> None:
    await _prepare_two_roots(wiring, tmp_path)

    result = await wiring.search.search("alpha", limit=10)
    repeated = await wiring.search.search("alpha", limit=10)

    uris = [str(merged.hit.logical_uri) for merged in result.content_hits]
    assert uris == [
        "local://university/notes/compilers.md",
        "vault://archive-main/courses/alpha-notes.md",
    ]
    assert all(merged.score == pytest.approx(1 / 61) for merged in result.content_hits)
    assert uris == [str(merged.hit.logical_uri) for merged in repeated.content_hits]


async def test_root_option_limits_the_search_to_one_root(
    wiring: Wiring, tmp_path: Path
) -> None:
    await _prepare_two_roots(wiring, tmp_path)

    result = await wiring.search.search("alpha", root_id="university", limit=10)

    assert [str(merged.hit.logical_uri) for merged in result.content_hits] == [
        "local://university/notes/compilers.md"
    ]
    assert all(str(entry.logical_uri).startswith("local://") for entry in result.metadata_hits)


async def test_metadata_hits_come_from_the_catalog_not_the_content(
    wiring: Wiring, tmp_path: Path
) -> None:
    await _prepare_two_roots(wiring, tmp_path)
    local = tmp_path / "documents" / "university"
    _write(local / "budget-2026.md", "unrelated body text\n")
    await wiring.catalog_service.scan_local(
        root_id="university", label="University", path=local
    )

    result = await wiring.search.search("budget", limit=10)

    assert result.content_hits == ()
    assert [str(entry.logical_uri) for entry in result.metadata_hits] == [
        "local://university/budget-2026.md"
    ]


async def test_offline_vault_keeps_metadata_hits_and_reports_offline(
    wiring: Wiring, tmp_path: Path
) -> None:
    _local, vault = await _prepare_two_roots(wiring, tmp_path)
    shutil.rmtree(vault)

    result = await wiring.search.search("alpha", limit=10)

    assert result.offline_roots == ("archive-main",)
    assert [str(merged.hit.logical_uri) for merged in result.content_hits] == [
        "local://university/notes/compilers.md"
    ]
    assert [str(entry.logical_uri) for entry in result.metadata_hits] == [
        "vault://archive-main/courses/alpha-notes.md"
    ]


async def test_a_different_vault_at_the_mount_is_reported_not_searched(
    wiring: Wiring, tmp_path: Path
) -> None:
    _local, vault = await _prepare_two_roots(wiring, tmp_path)
    shutil.rmtree(vault)
    vault.mkdir(parents=True)
    await wiring.manifests.initialize(vault, vault_id="someone-else", label="Other")
    _write(vault / "alpha-notes.md", "alpha from a different vault\n")

    result = await wiring.search.search("alpha", limit=10)

    assert result.identity_mismatches == ("archive-main",)
    assert all("archive-main" not in str(hit.hit.logical_uri) for hit in result.content_hits)


async def test_missing_entries_disappear_from_search(
    wiring: Wiring, tmp_path: Path
) -> None:
    local, _vault = await _prepare_two_roots(wiring, tmp_path)
    (local / "notes" / "compilers.md").unlink()
    await wiring.catalog_service.scan_local(
        root_id="university", label="University", path=local
    )
    await wiring.indexer.index_root("university")

    result = await wiring.search.search("alpha", limit=10)

    assert all("compilers.md" not in str(hit.hit.logical_uri) for hit in result.content_hits)
    assert all("compilers.md" not in str(entry.logical_uri) for entry in result.metadata_hits)


async def test_search_validates_query_and_limit(wiring: Wiring, tmp_path: Path) -> None:
    await _prepare_two_roots(wiring, tmp_path)

    with pytest.raises(ValueError, match="query"):
        await wiring.search.search("   ", limit=10)
    with pytest.raises(ValueError, match="limit"):
        await wiring.search.search("alpha", limit=0)
    with pytest.raises(ValueError, match="limit"):
        await wiring.search.search("alpha", limit=101)


async def test_snippets_are_bounded(wiring: Wiring, tmp_path: Path) -> None:
    local = tmp_path / "documents" / "university"
    long_line = "alpha " + "padding " * 400
    _write(local / "long.md", long_line + "\n")
    await wiring.catalog_service.scan_local(
        root_id="university", label="University", path=local
    )
    await wiring.indexer.index_root("university")

    result = await wiring.search.search("alpha", limit=5)

    assert result.content_hits
    for merged in result.content_hits:
        assert len(merged.hit.snippet) <= 310


async def test_context_search_returns_full_chunk_text_with_its_root(
    wiring: Wiring, tmp_path: Path
) -> None:
    """The grounded-answer read path: the indexed chunk itself, plus the root it came from."""
    await _prepare_two_roots(wiring, tmp_path)

    result = await wiring.search.search_context("alpha", limit=10)
    again = await wiring.search.search_context("alpha", limit=10)

    assert [str(hit.logical_uri) for hit in result.content_hits] == [
        "local://university/notes/compilers.md",
        "vault://archive-main/courses/alpha-notes.md",
    ]
    assert [hit.root_id for hit in result.content_hits] == ["university", "archive-main"]
    assert all("alpha" in hit.content for hit in result.content_hits)
    assert result.derived_queries == ("alpha",)  # a keyword matches verbatim, no fallback
    assert [hit.chunk_id for hit in result.content_hits] == [
        hit.chunk_id for hit in again.content_hits
    ]


async def test_context_search_answers_a_natural_language_question(
    wiring: Wiring, tmp_path: Path
) -> None:
    """A whole sentence misses as a phrase, so the same question is searched as keywords."""
    local = tmp_path / "documents" / "university"
    _write(
        local / "notes" / "notice.md",
        "Registration matters here.\nThe registration deadline is October 23.\n",
    )
    await wiring.catalog_service.scan_local(
        root_id="university", label="University", path=local
    )
    await wiring.indexer.index_root("university")

    result = await wiring.search.search_context(
        "What is the registration deadline for my course?", limit=5
    )

    assert [str(hit.logical_uri) for hit in result.content_hits] == [
        "local://university/notes/notice.md"
    ]
    assert "October 23" in result.content_hits[0].content
    assert result.derived_queries[0] == (
        "What is the registration deadline for my course?"
    )
    assert result.derived_queries[1:] == ("registration", "deadline", "course")


async def test_context_search_keyword_fallback_stays_deterministic(
    wiring: Wiring, tmp_path: Path
) -> None:
    local = tmp_path / "documents" / "university"
    _write(local / "notes" / "notice.md", "the registration deadline is October 23\n")
    _write(local / "notes" / "other.md", "an unrelated note about deadlines\n")
    await wiring.catalog_service.scan_local(
        root_id="university", label="University", path=local
    )
    await wiring.indexer.index_root("university")

    first = await wiring.search.search_context("when is the registration deadline?", limit=5)
    second = await wiring.search.search_context(
        "when is the registration deadline?", limit=5
    )

    assert [hit.chunk_id for hit in first.content_hits] == [
        hit.chunk_id for hit in second.content_hits
    ]
    assert first.derived_queries == second.derived_queries
