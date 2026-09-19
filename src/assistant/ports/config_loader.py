"""ConfigLoader port: read the host configuration (ADR-0013).

Reading a config file is blocking filesystem I/O, so it happens behind this async port and
the adapter performs it in a worker thread. The application never imports `tomllib`.
"""

from __future__ import annotations

from typing import Protocol

from assistant.domain.config import AssistantConfig


class ConfigLoader(Protocol):
    """Loads the host configuration."""

    async def load(self) -> AssistantConfig:
        """Return the configuration, or an empty one when the file does not exist.

        Raises:
            InvalidAssistantConfig: the file exists but cannot be trusted.
        """
        ...


__all__ = ["ConfigLoader"]

