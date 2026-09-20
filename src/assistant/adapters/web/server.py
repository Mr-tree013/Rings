"""The supervised ASGI server behind the mobile control plane (ADR-0026).

Two responsibilities, both about honesty:

- **it implements the daemon's `AsyncService`.** `run_forever` starts Uvicorn, waits on the stop
  event, and shuts the server down cleanly; a crash inside the web layer is just another supervised
  failure, and the rest of the daemon keeps running;
- **it decides who counts as local.** The private-client test is computed from the socket peer with
  `ipaddress` — loopback, private or link-local. No proxy header is consulted, because on a LAN a
  client that can set a header can claim anything.

The server is only built when `[mobile] enabled = true`; the CLI can describe the capability
without ever opening a socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import socket
from typing import Any

import uvicorn

from assistant.adapters.web.app import WebDependencies, build_app
from assistant.domain.mobile import MobileBindMode

LOGGER = logging.getLogger("assistant.mobile")

SHUTDOWN_GRACE_SECONDS = 5
"""How long Uvicorn may take to finish in-flight requests before the service returns."""


def is_private_client(peer: str) -> bool:
    """Whether a socket peer is on the local machine or the local network.

    Loopback, link-local and private ranges qualify; everything else is refused. A value that is
    not an address at all — an empty peer, a host name, a spoofed header — is refused too.
    """
    try:
        address = ipaddress.ip_address(peer.strip())
    except ValueError:
        return False
    return bool(address.is_loopback or address.is_private or address.is_link_local)


def lan_addresses() -> tuple[str, ...]:
    """Candidate LAN addresses a phone could use to reach this host.

    Best effort and purely informational: it reports the addresses the kernel knows about, and the
    CLI says as much rather than claiming the port is reachable.

    Every failure is swallowed — including a sandbox that refuses to open a socket at all — because
    a diagnostic that cannot run must not turn into an error. No packet is ever sent: the UDP
    `connect` only asks the kernel which local address a route would use.
    """
    candidates: list[str] = []
    with contextlib.suppress(Exception):
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            address = str(info[4][0])
            if address not in candidates and not address.startswith("127."):
                candidates.append(address)
    with contextlib.suppress(Exception):
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 9))
            address = probe.getsockname()[0]
            if address not in candidates and not address.startswith("127."):
                candidates.append(address)
        finally:
            probe.close()
    return tuple(candidates)


class MobileWebService:
    """The daemon service that serves the mobile control plane."""

    name = "mobile-web"

    def __init__(
        self,
        dependencies: WebDependencies,
        *,
        bind: MobileBindMode,
        port: int,
    ) -> None:
        self._dependencies = dependencies
        self._bind = bind
        self._port = port

    @property
    def host(self) -> str:
        """The address the server binds."""
        return self._bind.host

    @property
    def port(self) -> int:
        """The TCP port the server binds."""
        return self._port

    def build(self) -> Any:
        """Build the ASGI app without starting a server."""
        return build_app(self._dependencies, is_private=is_private_client)

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Serve until `stop_event` is set, then shut down cleanly.

        The Uvicorn server runs as a task so the stop event can end it; the task is always awaited
        so a shutdown never leaves a dangling server behind.
        """
        config = uvicorn.Config(
            self.build(),
            host=self.host,
            port=self._port,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
        server = uvicorn.Server(config)
        LOGGER.info(
            "mobile control plane listening bind=%s port=%d", self._bind.value, self._port
        )
        serving = asyncio.create_task(server.serve(), name="mobile-web:uvicorn")
        stopping = asyncio.create_task(stop_event.wait(), name="mobile-web:stop")
        try:
            done, _ = await asyncio.wait(
                {serving, stopping}, return_when=asyncio.FIRST_COMPLETED
            )
            if serving in done:
                # The server exited on its own: surface whatever it raised.
                serving.result()
        finally:
            stopping.cancel()
            server.should_exit = True
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                async with asyncio.timeout(SHUTDOWN_GRACE_SECONDS):
                    await serving
            with contextlib.suppress(asyncio.CancelledError):
                await stopping
        LOGGER.info("mobile control plane stopped")


__all__ = [
    "SHUTDOWN_GRACE_SECONDS",
    "MobileWebService",
    "is_private_client",
    "lan_addresses",
]
