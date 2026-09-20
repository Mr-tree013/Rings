"""Read-only observation of configured public pages (ADR-0029).

This package is the only place the project resolves a hostname or opens an HTTP connection outside
the mail and model adapters, and it does both under the watcher rules: public addresses only, no
redirects, no cookies, no authentication, no JavaScript, a finite timeout and a streaming byte cap.
There is no generic HTTP tool here — the interface takes a configured target and returns normalized
text, and nothing in the project can ask it for an arbitrary URL.
"""

from assistant.adapters.web_watch.http_source import HttpWebSource, default_resolver
from assistant.adapters.web_watch.snapshot_store import WebSnapshotStore

__all__ = ["HttpWebSource", "WebSnapshotStore", "default_resolver"]
