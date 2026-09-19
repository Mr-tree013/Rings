"""Filesystem adapters: vault manifests and metadata scanning (ADR-0011).

Everything here is blocking I/O wrapped in `asyncio.to_thread`; the application layer
never touches `os`, `os.scandir` or `pathlib.Path` traversal itself.
"""

