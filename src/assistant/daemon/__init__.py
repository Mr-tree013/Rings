"""`assistantd` — the long-running daemon (ADR-0001, ADR-0013).

The daemon owns supervised long-running services. Today that is the periodic storage index
reconciliation (`index-sync`) and the durable scheduler (`scheduler`), which delivers deadline
reminders and rolling replans; mail and web follow the same supervision shape when they exist.

The durable event worker exists but is deliberately not started here: there is still no real
business handler, and a no-op worker would only look like progress.
"""

from assistant.daemon.app import async_main, build_services, load_config, main, serve

__all__ = ["async_main", "build_services", "load_config", "main", "serve"]
