"""Test-wide guards.

The suite must be runnable offline and without spending provider credit, so outbound socket
connections are refused for every test. Provider behaviour is exercised through
`httpx.MockTransport`, and the one command that would call a live model (`pw model test`) is
always driven by an injected client or monkeypatched adapter.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Make any attempt to open a network connection fail loudly."""

    def refuse(self: socket.socket, *args: object, **kwargs: object) -> None:
        raise RuntimeError("tests must not open network connections")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    yield
