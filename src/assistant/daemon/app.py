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
from assistant.ports.model import ModelPort
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
    config: AssistantConfig,
    clock: Clock,
    database: Database,
    *,
    model: ModelPort | None = None,
) -> list[AsyncService]:
    """Compose the services the daemon supervises today.

    `index-sync` keeps derived views current; `scheduler` runs durable reminders and rolling
    replans; `mail-sync` receives inbound mail when at least one account is configured;
    `web-watch` observes the configured public pages; `mobile-web` serves the same-LAN control
    plane when `[mobile] enabled = true`; and `event-worker` analyzes what arrived — mail, web
    changes and pasted text — but only when the host can actually analyze it. Each one is its own
    supervisor, so a crashed web server cannot take the mail pipeline down with it.

    The last condition is the important one, and it is about the model rather than about mail. A
    worker that cannot reach a provider would dead-letter every received event, so a host with no
    usable model keeps receiving mail, watching pages and storing pasted text while accumulating
    `RECEIVED` events instead. Configuring a model and restarting drains that backlog; nothing is
    lost in the meantime, and a machine with no mail account at all still gets its manual input
    and page changes analyzed.
    """
    services: list[AsyncService] = [
        bootstrap.sync_service(config, clock, database),
        bootstrap.scheduler_service(database, clock, config),
    ]
    if config.mail.enabled_accounts:
        services.append(bootstrap.mail_sync_service(config, clock, database))
    if config.watchers.enabled_targets:
        services.append(bootstrap.web_watch_service(config, clock, database))
    if config.mobile.enabled:
        services.append(bootstrap.mobile_web_service(config, clock, database))
    if bootstrap.mail_analysis_available(config, model=model):
        services.append(bootstrap.mail_event_worker(config, clock, database, model=model))
    return services


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
