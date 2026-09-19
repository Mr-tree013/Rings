"""Extractor registry: a suffix lookup, deliberately not a framework (ADR-0012)."""

from __future__ import annotations

from collections.abc import Sequence

from assistant.adapters.content.pdf import PdfContentExtractor
from assistant.adapters.content.text import TextContentExtractor
from assistant.domain.catalog import CatalogEntry
from assistant.ports.content_extractor import ContentExtractor

DEFAULT_EXTRACTORS: tuple[ContentExtractor, ...] = (
    TextContentExtractor(),
    PdfContentExtractor(),
)


class SuffixExtractorRegistry:
    """Chooses the extractor whose whitelist covers an entry's suffix."""

    def __init__(self, extractors: Sequence[ContentExtractor] | None = None) -> None:
        self._extractors = tuple(extractors) if extractors is not None else DEFAULT_EXTRACTORS

    @property
    def extractors(self) -> tuple[ContentExtractor, ...]:
        """The extractors this registry consults, in order."""
        return self._extractors

    def extractor_for(self, entry: CatalogEntry) -> ContentExtractor | None:
        for extractor in self._extractors:
            if extractor.supports(entry):
                return extractor
        return None


def default_registry() -> SuffixExtractorRegistry:
    """The registry used in production: text files and PDFs."""
    return SuffixExtractorRegistry()


__all__ = ["DEFAULT_EXTRACTORS", "SuffixExtractorRegistry", "default_registry"]

