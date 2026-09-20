"""The vocabulary of an integrity check: sections, severity and a bounded report (ADR-0031).

An integrity report is a *summary*, not a dump. Each section carries a severity, one sentence and a
bounded list of short findings — entity kinds, counts, identifiers that are already public in the
CLI — and never a mail body, a page excerpt, a fact value, an action payload or a credential. A
checker that printed what it found would leak exactly the content the project spends its effort
protecting.

Nothing here can repair anything. The severities exist so a person can tell the difference between
"this is fine", "this is degraded but derived" (an offline vault, a missing cache) and "this is
broken and must not be used" (a tampered fingerprint, a referenced object that does not match its
hash).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

MAX_FINDINGS_PER_SECTION = 20
"""How many findings one section may carry, so a report stays readable and bounded."""


class IntegritySeverity(StrEnum):
    """How bad one section's result is."""

    OK = "ok"
    WARN = "warn"
    """Degraded but expected: an offline vault, a derived index that can be rebuilt."""
    FAIL = "fail"
    """Something referenced is missing or unreadable, and nothing repairs it automatically."""
    CRITICAL = "critical"
    """Stored state disagrees with itself: a tampered payload or a broken approval link."""


@dataclass(frozen=True, slots=True)
class IntegritySection:
    """One area of the check."""

    name: str
    severity: IntegritySeverity
    summary: str
    findings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("an integrity section needs a name")
        if len(self.findings) > MAX_FINDINGS_PER_SECTION:
            raise ValueError(
                f"a section may carry at most {MAX_FINDINGS_PER_SECTION} findings"
            )

    @property
    def ok(self) -> bool:
        """Whether this section found nothing worth acting on."""
        return self.severity is IntegritySeverity.OK


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    """Every section, in a stable order."""

    sections: tuple[IntegritySection, ...] = field(default_factory=tuple)

    @property
    def worst(self) -> IntegritySeverity:
        """The most serious severity any section reported."""
        order = (
            IntegritySeverity.OK,
            IntegritySeverity.WARN,
            IntegritySeverity.FAIL,
            IntegritySeverity.CRITICAL,
        )
        return max(
            (section.severity for section in self.sections),
            key=order.index,
            default=IntegritySeverity.OK,
        )

    @property
    def passed(self) -> bool:
        """Whether the check is a pass: nothing failed and nothing is critical."""
        return self.worst in (IntegritySeverity.OK, IntegritySeverity.WARN)

    @property
    def has_critical(self) -> bool:
        """Whether stored authority disagrees with itself."""
        return any(
            section.severity is IntegritySeverity.CRITICAL for section in self.sections
        )

    def section(self, name: str) -> IntegritySection | None:
        """One section by name, or `None`."""
        for item in self.sections:
            if item.name == name:
                return item
        return None


__all__ = [
    "MAX_FINDINGS_PER_SECTION",
    "IntegrityReport",
    "IntegritySection",
    "IntegritySeverity",
]
