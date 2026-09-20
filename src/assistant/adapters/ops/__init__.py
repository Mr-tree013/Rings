"""Operational adapters: reading the runtime's content objects by storage key (ADR-0031).

One small adapter, because the two content stores were built at different times and their roots
differ: raw mail lives under `<runtime>/mail/` and web snapshots under `<runtime>/`. The dispatcher
here is the single place that knows it, so the integrity checker can stay ignorant of layout.
"""

from assistant.adapters.ops.content_objects import RuntimeContentObjects

__all__ = ["RuntimeContentObjects"]
