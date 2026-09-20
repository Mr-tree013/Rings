"""The same-LAN mobile control plane: a small web UI over existing application services.

FastAPI, Starlette and Uvicorn live here and nowhere else, and this package cannot reach an
executor: it reviews, approves, edits a draft and creates a task — it never sends, submits or
runs anything (ADR-0026).
"""

from assistant.adapters.web.app import WebDependencies, build_app
from assistant.adapters.web.server import MobileWebService, lan_addresses

__all__ = ["MobileWebService", "WebDependencies", "build_app", "lan_addresses"]
