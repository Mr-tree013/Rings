"""Daemon composition and lifecycle (ADR-0013).

Startup is deliberately ordered: load the host config, open the runtime database and migrate
it synchronously, compose services, then supervise them. A configuration or migration failure
is fatal (the user must fix it); a *service* failure is not (the supervisor retries it).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Sequence
from pathlib import Path

from assistant import bootstrap
from assistant.daemon.supervisor import AsyncService, supervise
from assistant.domain.config import AssistantConfig
from assistant.domain.errors import InvalidAssistantConfig
from assistant.ports.clock import Clock
from assistant.store.db import Database

LOGGER = logging.getLogger("assistant.daemon")


def configure_logging(level: int = logging.INFO) -> None:
    """Configure daemon logging (stdout, one line per record)."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    """Ask asyncio to set `stop_event` when the process is asked to terminate."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError, ValueError):  # pragma: no cover
            LOGGER.debug("signal handler for %s is unavailable on this platform", sig)


def build_services(
    config: AssistantConfig, clock: Clock, database: Database
) -> list[AsyncService]:
    """Compose the services the daemon supervises today.

    `index-sync` keeps derived views current; `scheduler` runs durable reminders and rolling
    replans. The durable `EventWorker` is deliberately absent: there is still no real inbound
    handler, and a fake handler would only pretend that mail is being processed.
    """
    return [
        bootstrap.sync_service(config, clock, database),
        bootstrap.scheduler_service(database, clock, config),
    ]


async def load_config(config_path: Path | None = None) -> AssistantConfig:
    """Read and validate the host configuration."""
    return await bootstrap.config_loader(config_path).load()


async def serve(
    stop_event: asyncio.Event, services: Sequence[AsyncService] = ()
) -> None:
    """Supervise `services` until `stop_event` is set.

    Each service runs inside its own supervisor task, so one service's failure never cancels
    its siblings. Cancellation propagates: this coroutine never swallows `CancelledError`.
    """
    if not services:
        await stop_event.wait()
        return
    async with asyncio.TaskGroup() as group:
        for service in services:
            group.create_task(
                supervise(service, stop_event), name=f"supervisor:{service.name}"
            )


async def async_main(config_path: Path | None = None) -> None:
    """Async entry point: load config, prepare storage, supervise services."""
    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)
    clock = bootstrap.system_clock()
    config = await load_config(config_path)
    LOGGER.info(
        "configuration loaded: %d roots (%d enabled), interval %ds, run_on_startup=%s",
        len(config.roots),
        len(config.enabled_roots),
        config.indexing.interval_seconds,
        config.indexing.run_on_startup,
    )
    database = bootstrap.runtime_database(clock)
    services = build_services(config, clock, database)
    LOGGER.info(
        "assistantd starting (pid=%d, services=%s)",
        os.getpid(),
        ",".join(service.name for service in services),
    )
    try:
        await serve(stop_event, services)
    finally:
        LOGGER.info("assistantd shutting down")


def main() -> None:
    """Console-script entry point (`assistantd`)."""
    configure_logging()
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:  # pragma: no cover - fallback when signal handlers are unavailable
        LOGGER.info("assistantd interrupted")
    except InvalidAssistantConfig as exc:
        LOGGER.error("invalid configuration: %s", exc)
        raise SystemExit(2) from exc
    except Exception as exc:  # fatal startup failure: the user has to fix it
        LOGGER.error("assistantd failed to start: %s: %s", type(exc).__name__, exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "async_main",
    "build_services",
    "configure_logging",
    "load_config",
    "main",
    "serve",
]
