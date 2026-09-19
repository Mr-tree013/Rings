"""`assistantd` — the long-running daemon (ADR-0001, ADR-0013).

The daemon owns supervised long-running services. Today that is exactly one service, the
periodic storage index reconciliation (`index-sync`); mail, web and scheduler follow the same
supervision shape when they exist.

The durable event worker exists but is deliberately not started here: there is still no real
business handler, and a no-op worker would only look like progress.
"""

from assistant.daemon.app import async_main, main, serve

__all__ = ["async_main", "main", "serve"]

