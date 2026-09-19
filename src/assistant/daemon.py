"""`assistantd` — the long-running asyncio daemon.

Current scope: process lifecycle only. The daemon starts, waits for a stop signal, and
shuts down cleanly. The four long-running services (mail, index, web, scheduler) are not
implemented and must not be faked here; when they arrive they are supervised from
`serve()`, each inside its own exception boundary so that one failing service cannot tear
down the daemon (see docs/specs/0001-system-design.md).

The durable event worker exists but is deliberately not started here: there is no real
handler yet, and a no-op worker would only look like progress.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

LOGGER = logging.getLogger("assistant.daemon")


def _configure_logging(level: int = logging.INFO) -> None:
    """Configure basic logging for the daemon process."""
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


async def serve(stop_event: asyncio.Event) -> None:
    """Run the daemon body until `stop_event` is set.

    Later phases add one supervised task per service here. Cancellation propagates: this
    coroutine never swallows `asyncio.CancelledError`.
    """
    LOGGER.info("assistantd starting (pid=%d)", os.getpid())
    try:
        await stop_event.wait()
    finally:
        LOGGER.info("assistantd shutting down")


async def async_main() -> None:
    """Async entry point: install signal handling, then run `serve`."""
    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)
    await serve(stop_event)


def main() -> None:
    """Console-script entry point (`assistantd`)."""
    _configure_logging()
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:  # pragma: no cover - fallback when signal handlers are unavailable
        LOGGER.info("assistantd interrupted")


if __name__ == "__main__":  # pragma: no cover
    main()
