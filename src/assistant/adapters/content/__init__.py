"""Content adapters: safe file access, text extraction and PDF extraction (ADR-0012).

Everything here is blocking I/O wrapped in `asyncio.to_thread` by the extractors. The only
module allowed to import a third-party parser (`pypdf`) is `pdf.py`.
"""

