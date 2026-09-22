"""Daemon composition and lifecycle (ADR-0013).

Startup is deliberately ordered: load the host config, open the runtime database and migrate
it synchronously, compose services, then supervise them. A configuration or migration failure
is fatal (the user must fix it); a *service* failure is not (the supervisor retries it).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from assistant import bootstrap
from assistant.adapters.config.secrets_env import load_secret_environment_into_process
from assistant.adapters.runtime.instance_lock import (
    InstanceLock,
    InstanceLockHeld,
    daemon_lock_path,
)
from assistant.daemon.attention_service import AttentionRefreshService
from assistant.daemon.conversation_queue import ConversationQueueService
from assistant.daemon.supervisor import AsyncService, supervise
from assistant.domain.config import AssistantConfig
from assistant.domain.errors import (
    InvalidAssistantConfig,
    ModelCredentialsMissing,
    ModelNotConfigured,
)
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
    config_path: Path | None = None,
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
    broker = bootstrap.conversation_event_broker()
    # One broker for the whole process, so an attention change reaches the pages the chat service
    # is already serving instead of a second, parallel channel nobody is listening to.
    services: list[AsyncService] = [
        bootstrap.sync_service(config, clock, database),
        bootstrap.scheduler_service(database, clock, config),
        AttentionRefreshService(
            bootstrap.attention_projector(database, clock, config),
            broker=broker,
        ),
    ]
    if config.mail.enabled_accounts:
        services.append(bootstrap.mail_sync_service(config, clock, database))
    if config.watchers.enabled_targets:
        services.append(bootstrap.web_watch_service(config, clock, database))
    if config.mobile.enabled:
        chat = None
        with contextlib.suppress(ModelNotConfigured, ModelCredentialsMissing):
            chat = bootstrap.conversation_chat_service(
                database, clock, config, model=model, broker=broker
            )
        services.append(
            bootstrap.mobile_web_service(
                config, clock, database, chat=chat, config_path=config_path
            )
        )
        if chat is not None:
            # The durable queue needs an owner that survives restarts: recovery on start, and only
            # queued work resumed (ADR-0041 §10).
            services.append(ConversationQueueService(chat.coordinator))
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
    """Async entry point: take the instance lock, then serve until asked to stop."""
    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)
    clock = bootstrap.system_clock()
    lock = _acquire_instance_lock()
    try:
        await _serve_runtime(config_path, clock, stop_event)
    finally:
        # Released last, after every supervised service has stopped: a successor started in the
        # same second must not find a half-serving predecessor still writing to the runtime.
        lock.release()
        LOGGER.info("assistantd shut down")


def _acquire_instance_lock() -> InstanceLock:
    """Take the per-runtime daemon lock, or raise `InstanceLockHeld` (ADR-0032).

    The lock is held for the whole process lifetime; the kernel releases it when the process ends.
    A lock file without a held lock is merely an old file, so a machine that lost power does not
    need anybody to delete anything before the daemon can start again.
    """
    runtime = bootstrap.AppPaths.resolve().runtime
    lock = InstanceLock(
        daemon_lock_path(runtime),
        metadata={
            "pid": os.getpid(),
            "started_at": bootstrap.system_clock().now().isoformat(),
            "version": bootstrap.assistant_version(),
            "runtime_root": str(runtime),
        },
    )
    lock.acquire()
    LOGGER.info("instance lock acquired (%s)", lock.path.name)
    return lock


async def _serve_runtime(
    config_path: Path | None, clock: Clock, stop_event: asyncio.Event
) -> None:
    """Load the host configuration, prepare the runtime and supervise its services."""
    config = await load_config(config_path)
    LOGGER.info(
        "configuration loaded: %d roots (%d enabled), interval %ds, run_on_startup=%s",
        len(config.roots),
        len(config.enabled_roots),
        config.indexing.interval_seconds,
        config.indexing.run_on_startup,
    )
    database = bootstrap.runtime_database(clock)
    services = build_services(config, clock, database, config_path=config_path)
    LOGGER.info(
        "assistantd starting (pid=%d, services=%s)",
        os.getpid(),
        ",".join(service.name for service in services),
    )
    try:
        await serve(stop_event, services)
    finally:
        LOGGER.info("assistantd stopping services")


def main() -> None:
    """Console-script entry point (`assistantd`)."""
    # The user's own secrets file first, so the services this process composes find credentials
    # without anyone exporting them by hand (ADR-0046).
    load_secret_environment_into_process(model_key=bootstrap.MODEL_API_KEY_ENV)
    arguments = sys.argv[1:]
    if arguments == ["--version"]:
        sys.stdout.write(f"assistantd {bootstrap.assistant_version()}\n")
        raise SystemExit(0)
    if arguments:
        sys.stderr.write(
            f"assistantd: unknown argument(s): {' '.join(arguments)}; "
            "the daemon takes no options except --version\n"
        )
        raise SystemExit(2)
    configure_logging()
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:  # pragma: no cover - fallback when signal handlers are unavailable
        LOGGER.info("assistantd interrupted")
    except InstanceLockHeld as exc:
        LOGGER.error("%s", exc)
        raise SystemExit(3) from exc
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
