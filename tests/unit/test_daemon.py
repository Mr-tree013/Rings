"""Tests for the daemon lifecycle and its startup composition (ADR-0013).

The daemon is always started against temporary XDG directories: no test may touch the real
user config, runtime database or cache.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from assistant.daemon import async_main, serve


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def _write_config(tmp_path: Path, root_path: Path, *, interval_seconds: int = 3600) -> None:
    config_directory = tmp_path / "config" / "growing-assistant"
    config_directory.mkdir(parents=True, exist_ok=True)
    (config_directory / "config.toml").write_text(
        "\n".join(
            (
                "format_version = 1",
                "",
                "[indexing]",
                f"interval_seconds = {interval_seconds}",
                "run_on_startup = true",
                "",
                "[[storage.roots]]",
                'kind = "local"',
                'id = "university"',
                'label = "University"',
                f'path = "{root_path}"',
                "enabled = true",
                "",
            )
        ),
        encoding="utf-8",
    )


async def test_serve_returns_when_stop_event_is_set() -> None:
    stop_event = asyncio.Event()
    task = asyncio.create_task(serve(stop_event))

    await asyncio.sleep(0)  # let the daemon body reach its wait state
    assert not task.done()

    stop_event.set()
    await asyncio.wait_for(task, timeout=1)

    assert not task.cancelled()
    assert task.exception() is None


async def test_serve_propagates_cancellation() -> None:
    stop_event = asyncio.Event()
    task = asyncio.create_task(serve(stop_event))

    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


async def test_async_main_starts_and_stops_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch, tmp_path)
    task = asyncio.create_task(async_main())

    await asyncio.sleep(0.1)
    assert not task.done()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The only outcome is the cancellation we requested: Task.exception() itself raises
    # CancelledError for a cancelled task, so `cancelled()` is the meaningful assertion.
    assert task.cancelled()
    leftover = [
        item
        for item in asyncio.all_tasks()
        if item is not asyncio.current_task() and not item.done()
    ]
    assert leftover == []


async def test_async_main_runs_an_initial_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Startup order: config, migrations, first reconciliation, then periodic waiting."""
    _isolate(monkeypatch, tmp_path)
    documents = tmp_path / "documents" / "university"
    documents.mkdir(parents=True)
    (documents / "notes.md").write_text("the important deadline is Friday\n", encoding="utf-8")
    _write_config(tmp_path, documents)
    task = asyncio.create_task(async_main())

    catalog = tmp_path / "data" / "growing-assistant" / "assistant.db"
    index = (
        tmp_path
        / "cache"
        / "growing-assistant"
        / "knowledge"
        / "university"
        / "index.sqlite3"
    )
    try:
        await _wait_for(lambda: _indexed_documents(catalog, index) == 1, timeout=15)
        states = await _query(index, "SELECT relative_path, status FROM knowledge_documents")
        roots = await _query(catalog, "SELECT root_id, kind FROM storage_roots")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert roots == [("university", "local")]
    assert states == [("notes.md", "indexed")]


def _indexed_documents(catalog: Path, index: Path) -> int:
    """How many documents the index holds so far (0 while it is still being built)."""
    if not catalog.is_file() or not index.is_file():
        return 0
    try:
        with sqlite3.connect(index) as connection:
            row = connection.execute("SELECT count(*) FROM knowledge_documents").fetchone()
    except sqlite3.Error:
        return 0
    return 0 if row is None else int(row[0])


async def _wait_for(predicate: object, *, timeout: float) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():  # type: ignore[operator]
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition was not met before the timeout")


async def _query(path: Path, statement: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as connection:
        return [tuple(row) for row in connection.execute(statement)]
