"""The daemon's web service: composed only when enabled, supervised like anything else (ADR-0026).

Two properties are worth a real test. The control plane is *opt-in* — a host that never wrote a
`[mobile]` section supervises exactly the services it always did — and it is *isolated*: the web
service is one supervisor among several, so a socket that cannot be bound is retried while the
rest of the daemon keeps working.

No test here opens a socket. The Uvicorn objects are replaced by a fake that records what it was
asked to do, which is also how "the stop event really stops it" is proven.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

import pytest
import uvicorn

from assistant import bootstrap
from assistant.adapters.web.server import MobileWebService
from assistant.daemon import load_config
from assistant.daemon.app import build_services, serve
from assistant.domain.mobile import MobileBindMode

BASE_CONFIG = "\n".join(
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
    )
)

MOBILE_SECTION = "\n".join(
    (
        "[mobile]",
        "enabled = true",
        'bind = "lan"',
        "port = 8765",
        "",
    )
)


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, enabled: bool) -> None:
    """Write a host config in a private XDG tree, with or without the control plane."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    directory = tmp_path / "config" / "growing-assistant"
    directory.mkdir(parents=True, exist_ok=True)
    text = BASE_CONFIG + (MOBILE_SECTION if enabled else "")
    (directory / "config.toml").write_text(text, encoding="utf-8")


async def _wait_until(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition was not met before the timeout")


class _FakeUvicornServer:
    """A serving loop that watches `should_exit`, like the real one, and touches nothing."""

    instances: ClassVar[list[_FakeUvicornServer]] = []

    def __init__(self, config: Any) -> None:
        self.config = config
        self.should_exit = False
        self.started = asyncio.Event()
        self.stopped = False
        self.exc: BaseException | None = None
        _FakeUvicornServer.instances.append(self)

    async def serve(self) -> None:
        self.started.set()
        if self.exc is not None:
            raise self.exc
        while not self.should_exit:
            await asyncio.sleep(0.01)
        self.stopped = True


class _FakeUvicornConfig:
    """Records the arguments the wrapper passed, without resolving an address."""

    def __init__(self, app: Any, **kwargs: Any) -> None:
        self.app = app
        self.kwargs = kwargs


@pytest.fixture
def fake_uvicorn(monkeypatch: pytest.MonkeyPatch) -> type[_FakeUvicornServer]:
    _FakeUvicornServer.instances = []
    monkeypatch.setattr(uvicorn, "Config", _FakeUvicornConfig)
    monkeypatch.setattr(uvicorn, "Server", _FakeUvicornServer)
    return _FakeUvicornServer


async def _web_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MobileWebService:
    """The composed service the daemon would supervise, over a real config and database."""
    _isolate(monkeypatch, tmp_path, enabled=True)
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    return bootstrap.mobile_web_service(await load_config(), clock, database)


async def test_the_control_plane_is_absent_unless_it_is_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host that never asked for the mobile plane supervises the services it always did."""
    _isolate(monkeypatch, tmp_path, enabled=False)
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)

    config = await load_config()

    assert config.mobile.enabled is False
    assert [service.name for service in build_services(config, clock, database)] == [
        "index-sync",
        "scheduler",
        "attention",
    ]


async def test_enabling_the_control_plane_adds_one_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch, tmp_path, enabled=True)
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)

    config = await load_config()
    services = build_services(config, clock, database)

    assert [service.name for service in services] == [
        "index-sync",
        "scheduler",
        "attention",
        "mobile-web",
    ]
    web = services[-1]
    assert isinstance(web, MobileWebService)
    assert web.host == "0.0.0.0"
    assert web.port == 8765


async def test_the_server_stops_cleanly_on_the_stop_event(
    fake_uvicorn: type[_FakeUvicornServer], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stop event ends the server, the task is awaited, and nothing is left dangling."""
    web = await _web_service(tmp_path, monkeypatch)
    stop_event = asyncio.Event()

    running = asyncio.create_task(web.run_forever(stop_event))
    await _wait_until(lambda: bool(fake_uvicorn.instances))
    server = fake_uvicorn.instances[-1]
    await asyncio.wait_for(server.started.wait(), timeout=10)
    assert not running.done()

    stop_event.set()
    await asyncio.wait_for(running, timeout=10)

    assert server.should_exit is True
    assert server.stopped is True
    leftovers = [
        task for task in asyncio.all_tasks() if task.get_name().startswith("mobile-web:")
    ]
    assert not leftovers


async def test_the_wrapper_never_logs_access_lines(
    fake_uvicorn: type[_FakeUvicornServer], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Access logs would carry route paths and every chance to leak a token into a file."""
    web = await _web_service(tmp_path, monkeypatch)
    stop_event = asyncio.Event()
    running = asyncio.create_task(web.run_forever(stop_event))
    await _wait_until(lambda: bool(fake_uvicorn.instances))
    server = fake_uvicorn.instances[-1]
    await asyncio.wait_for(server.started.wait(), timeout=10)
    stop_event.set()
    await asyncio.wait_for(running, timeout=10)

    assert server.config.kwargs["access_log"] is False
    assert server.config.kwargs["log_level"] == "warning"
    assert server.config.kwargs["host"] == "0.0.0.0"
    assert server.config.kwargs["port"] == 8765
    # The app the server was handed is the mobile one: a real FastAPI route table, no docs.
    assert server.config.app.docs_url is None


async def test_the_bound_host_follows_the_bind_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch, tmp_path, enabled=True)
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    config = await load_config()
    dependencies = bootstrap.mobile_web_dependencies(config, clock, database)

    lan = MobileWebService(dependencies, bind=MobileBindMode.LAN, port=8765)
    loopback = MobileWebService(dependencies, bind=MobileBindMode.LOOPBACK, port=8765)

    assert (lan.host, loopback.host) == ("0.0.0.0", "127.0.0.1")


async def test_a_web_failure_is_retried_and_its_siblings_survive(
    fake_uvicorn: type[_FakeUvicornServer], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A port already in use is the web service's problem, not the daemon's."""
    web = await _web_service(tmp_path, monkeypatch)
    stop_event = asyncio.Event()
    sibling_ran = asyncio.Event()
    sibling_stopped = asyncio.Event()

    class _Sibling:
        name = "index-sync"

        async def run_forever(self, stop_event: asyncio.Event) -> None:
            sibling_ran.set()
            await stop_event.wait()
            sibling_stopped.set()

    # The first bind fails; later attempts serve normally, so the restart is observable.
    original_init = _FakeUvicornServer.__init__

    def _init(self: _FakeUvicornServer, config: Any) -> None:
        original_init(self, config)
        if len(_FakeUvicornServer.instances) == 1:
            self.exc = RuntimeError("address already in use")

    monkeypatch.setattr(_FakeUvicornServer, "__init__", _init)

    supervising = asyncio.create_task(serve(stop_event, [web, _Sibling()]))
    await asyncio.wait_for(sibling_ran.wait(), timeout=10)
    await _wait_until(lambda: len(_FakeUvicornServer.instances) >= 2)

    # The web service was restarted; the sibling was never cancelled, and the daemon is alive.
    assert not supervising.done()
    assert not stop_event.is_set()
    assert not sibling_stopped.is_set()

    stop_event.set()
    await asyncio.wait_for(supervising, timeout=10)
    assert sibling_stopped.is_set()
