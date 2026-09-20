"""The daemon's mail intelligence: composition, gating and sibling isolation (ADR-0021).

The `event-worker` is the first real handler, and the rule that matters most here is *when it is
absent*: a host with mail accounts but no usable model keeps receiving mail and accumulating
`RECEIVED` events, because a worker that cannot reach a provider would only dead-letter them.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from assistant import bootstrap
from assistant.adapters.model.fake import FakeModelAdapter
from assistant.daemon import load_config, serve
from assistant.daemon.app import build_services
from assistant.daemon.supervisor import BackoffPolicy, supervise
from assistant.domain.config import MailConfig, ModelConfig
from assistant.domain.inbound_event import EventStatus
from tests.support.mail_intelligence import MailStores, build_message

CONFIG = "\n".join(
    (
        "format_version = 1",
        "",
        "[indexing]",
        "interval_seconds = 3600",
        "",
        "[planning]",
        'timezone = "Asia/Shanghai"',
        "",
        "[scheduler]",
        "poll_interval_seconds = 300",
        "",
        "[[mail.accounts]]",
        'id = "smail"',
        'host = "imap.example.edu"',
        "port = 993",
        'username = "student@example.edu"',
        'mailbox = "INBOX"',
        "enabled = true",
        "",
    )
)


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    directory = tmp_path / "config" / "growing-assistant"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.toml").write_text(CONFIG, encoding="utf-8")


def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """The daemon must never open a socket in these tests."""

    class _Unreachable:
        async def fetch(self, request: object) -> object:
            raise AssertionError("the daemon must not contact a mail server here")

    monkeypatch.setattr(bootstrap, "mail_source", lambda account, **kwargs: _Unreachable())


async def _wait_for(condition: object, timeout: float = 10.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if await condition():  # type: ignore[operator]
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition was not met before the timeout")


async def _wait_until(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition was not met before the timeout")


async def test_no_model_means_no_event_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a model a worker would only dead-letter mail, so it is not started."""
    _isolate(monkeypatch, tmp_path)
    _no_network(monkeypatch)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    clock = bootstrap.system_clock()
    config = await load_config()
    database = bootstrap.runtime_database(clock)

    assert bootstrap.mail_analysis_available(config) is False
    assert [service.name for service in build_services(config, clock, database)] == [
        "index-sync",
        "scheduler",
        "mail-sync",
    ]


async def test_a_configured_model_starts_the_event_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch, tmp_path)
    _no_network(monkeypatch)
    clock = bootstrap.system_clock()
    config = replace(await load_config(), model=ModelConfig())
    database = bootstrap.runtime_database(clock)
    model = FakeModelAdapter()

    services = build_services(config, clock, database, model=model)

    assert [service.name for service in services] == [
        "index-sync",
        "scheduler",
        "mail-sync",
        "event-worker",
    ]


async def test_the_worker_starts_without_mail_accounts_once_a_model_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§31 of Phase 8A: the worker's precondition is a usable model, not a mail account.

    Mail is no longer the only thing that produces events — a watched page and `pw ingest text`
    do too — so a host with no mail account still needs something to analyze them. Without a model
    the worker is still absent, and the events simply stay `RECEIVED`.
    """
    _isolate(monkeypatch, tmp_path)
    clock = bootstrap.system_clock()
    config = replace(await load_config(), model=ModelConfig(), mail=MailConfig())
    database = bootstrap.runtime_database(clock)

    with_model = build_services(config, clock, database, model=FakeModelAdapter())
    without_model = build_services(
        replace(config, model=None), clock, database, model=None
    )

    assert [service.name for service in with_model] == [
        "index-sync",
        "scheduler",
        "event-worker",
    ]
    assert [service.name for service in without_model] == ["index-sync", "scheduler"]


async def test_a_received_mail_is_analyzed_by_the_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole daemon, with a scripted provider: mail in, durable analysis out."""
    _isolate(monkeypatch, tmp_path)
    _no_network(monkeypatch)
    clock = bootstrap.system_clock()
    config = replace(await load_config(), model=ModelConfig())
    database = bootstrap.runtime_database(clock)
    stores = MailStores(database, clock)
    message = await stores.store(build_message(message_id_header="<a@example.edu>"))
    event = await stores.bridge(message)
    model = FakeModelAdapter().queue_text(
        json.dumps(
            {
                "category": "ordinary_correspondence",
                "requires_reply": False,
                "summary": "A note from a colleague.",
                "action_candidates": [],
            }
        )
    )
    services = build_services(config, clock, database, model=model)
    stop_event = asyncio.Event()
    daemon = asyncio.create_task(serve(stop_event, services))

    async def analyzed() -> bool:
        return await stores.intelligence.get_analysis(message.id) is not None

    try:
        await _wait_for(analyzed)
    finally:
        stop_event.set()
        await asyncio.wait_for(daemon, timeout=10)

    assert (await stores.events.get(event.id)).status is EventStatus.PROCESSED
    assert len(model.requests) == 1
    assert (await stores.mail.get_message(message.id)) == message


async def test_mail_without_a_model_stays_received(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backlog is not lost and not dead-lettered: it waits for a model to be configured."""
    _isolate(monkeypatch, tmp_path)
    _no_network(monkeypatch)
    clock = bootstrap.system_clock()
    config = await load_config()
    database = bootstrap.runtime_database(clock)
    stores = MailStores(database, clock)
    message = await stores.store(build_message(message_id_header="<a@example.edu>"))
    event = await stores.bridge(message)
    stop_event = asyncio.Event()
    daemon = asyncio.create_task(serve(stop_event, build_services(config, clock, database)))

    try:
        await asyncio.sleep(0.2)
    finally:
        stop_event.set()
        await asyncio.wait_for(daemon, timeout=10)

    assert (await stores.events.get(event.id)).status is EventStatus.RECEIVED
    assert await stores.intelligence.get_analysis(message.id) is None


async def test_a_mail_service_failure_does_not_cancel_its_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One supervisor's crash loop stays inside its own supervisor."""
    _isolate(monkeypatch, tmp_path)
    clock = bootstrap.system_clock()
    config = replace(await load_config(), model=ModelConfig())
    database = bootstrap.runtime_database(clock)
    index_sync = bootstrap.sync_service(config, clock, database)
    mail = bootstrap.mail_sync_service(config, clock, database)
    worker = bootstrap.mail_event_worker(
        config, clock, database, model=FakeModelAdapter()
    )
    runs = {"sync": 0, "mail": 0, "worker": 0}
    original_sync = index_sync.run_forever
    original_worker = worker.run_forever

    async def exploding_mail(stop_event: asyncio.Event) -> None:
        del stop_event
        runs["mail"] += 1
        raise RuntimeError("mail infrastructure exploded")

    async def counting_worker(stop_event: asyncio.Event) -> None:
        runs["worker"] += 1
        await original_worker(stop_event)

    async def counting_sync(stop_event: asyncio.Event) -> None:
        runs["sync"] += 1
        await original_sync(stop_event)

    index_sync.run_forever = counting_sync  # type: ignore[method-assign]
    mail.run_forever = exploding_mail  # type: ignore[method-assign]
    worker.run_forever = counting_worker  # type: ignore[method-assign]
    stop_event = asyncio.Event()
    policy = BackoffPolicy(base_delay_seconds=0.01, max_delay_seconds=0.05)

    async with asyncio.TaskGroup() as group:
        group.create_task(supervise(index_sync, stop_event, policy=policy))
        group.create_task(supervise(mail, stop_event, policy=policy))
        group.create_task(supervise(worker, stop_event, policy=policy))
        await _wait_until(
            lambda: runs["sync"] >= 1 and runs["mail"] >= 2 and runs["worker"] >= 1
        )
        stop_event.set()

    assert runs["sync"] == 1  # index-sync was never restarted or cancelled
    assert runs["worker"] == 1  # and neither was the worker
    assert runs["mail"] > 1  # the failing service was restarted
