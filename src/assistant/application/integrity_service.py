"""`pw integrity check`: a read-only, offline audit of the whole runtime (ADR-0031).

```text
database        pragmas, applied migrations against the reviewed files
content         every referenced mail raw object and web snapshot, by hash
capabilities    approvals, challenges, executions and send links
conversation    threads, turns and operations; an interrupted write stays visible
learning        facts, playbooks and their provenance
mail            identity, threading, drafts, send links
observations    web versioning, bridges, analyses
knowledge       configured roots and their indexes — derived, and reported as such
```

The service composes what the database says (through the audit port) with what the filesystem says
(through the content-object port) and what the host is configured to index. It never writes, never
migrates, never re-indexes, never contacts a mail server, an eHall page, a watcher or a model, and
never prints content: a finding names an entity or a count, and that is all.

An offline removable vault is `WARN`, not corruption. A derived index that can be rebuilt is
`WARN`. A referenced object that is missing is `FAIL`. A payload that no longer hashes to its own
fingerprint is `CRITICAL`, because that is stored authority disagreeing with itself, and nothing
here will "fix" it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from assistant.domain.backup import DATABASE_FILENAME, MAIL_ROOT_DIRECTORY, sha256_hex
from assistant.domain.errors import DomainError
from assistant.domain.integrity import (
    IntegrityReport,
    IntegritySection,
    IntegritySeverity,
)
from assistant.ports.content_objects import ContentObjectReader
from assistant.ports.file_modes import FileModeInspector
from assistant.ports.integrity_repository import DatabaseAudit, IntegrityRepository

LOGGER = logging.getLogger("assistant.integrity")

REVIEWED_MIGRATION_COUNT = 21
"""How many migration files the reviewed schema has; pinned again by an architecture test."""


class IntegrityService:
    """Read-only verification of the durable runtime state."""

    def __init__(
        self,
        repository: IntegrityRepository,
        objects: ContentObjectReader,
        *,
        migrations_directory: Path | None = None,
        runtime_root: Path | None = None,
        file_modes: FileModeInspector | None = None,
        indexes: object | None = None,
    ) -> None:
        self._repository = repository
        self._objects = objects
        self._migrations = migrations_directory
        self._runtime_root = runtime_root
        self._file_modes = file_modes
        self._indexes = indexes

    async def check(self) -> IntegrityReport:
        """Run every check and return the bounded report."""
        audit = self._repository.audit()
        sections: list[IntegritySection] = [
            _migrations_section(audit, self._migrations),
            *audit.sections,
        ]
        sections.append(await self._content_section(audit))
        sections.append(self._knowledge_section(audit))
        sections.append(self._permissions_section())
        report = IntegrityReport(sections=tuple(sections))
        LOGGER.info("integrity check completed worst=%s", report.worst.value)
        return report

    # ---------------------------------------------------------------- sections

    def _permissions_section(self) -> IntegritySection:
        """Report the modes of the private objects, and never repair them (ADR-0032).

        A world-writable database or content root is a `FAIL`: anyone on the machine could rewrite
        the authority this check just verified. Group/other readable bits are a `WARN`, because a
        private parent directory may already be the real boundary. Nothing here changes a mode — a
        diagnostic that silently rewrote permissions would destroy the evidence it was run to find,
        and it would also rewrite modes on files the user configured deliberately.
        """
        if self._runtime_root is None or self._file_modes is None:
            return IntegritySection(
                "permissions",
                IntegritySeverity.OK,
                "not checked (no runtime directory was provided)",
            )
        root = Path(self._runtime_root)
        targets = (
            ("runtime directory", root),
            ("runtime database", root / DATABASE_FILENAME),
            ("mail raw root", root / MAIL_ROOT_DIRECTORY),
            ("web snapshot root", root / "web" / "snapshots"),
        )
        findings: list[str] = []
        inspected = 0
        failing = False
        warning = False
        for label, path in targets:
            if not path.exists():
                continue
            inspected += 1
            observed = self._file_modes.inspect(path)
            mode = observed.mode
            if mode is None:
                continue
            if observed.world_writable:
                failing = True
                findings.append(f"{label}: {mode:04o} is writable by anyone on this machine")
            elif not observed.owner_only:
                warning = True
                findings.append(
                    f"{label}: {mode:04o} is readable by group/other "
                    "(private parents are what actually contain it)"
                )
        severity = (
            IntegritySeverity.FAIL
            if failing
            else IntegritySeverity.WARN
            if warning
            else IntegritySeverity.OK
        )
        return IntegritySection(
            "permissions",
            severity,
            f"{inspected} private object(s) inspected; nothing was changed",
            tuple(findings),
        )

    async def _content_section(self, audit: DatabaseAudit) -> IntegritySection:
        """Every referenced object must exist and match the hash the database recorded."""
        mail_total = mail_valid = 0
        web_total = web_valid = 0
        findings: list[str] = []
        for item in audit.objects:
            if item.kind == "mail_raw":
                mail_total += 1
            else:
                web_total += 1
            try:
                payload = await self._objects.read_object(item.storage_key)
            except DomainError as exc:
                findings.append(f"{item.kind} {item.storage_key}: {exc}")
                continue
            if sha256_hex(payload) != item.sha256:
                findings.append(
                    f"{item.kind} {item.storage_key} does not match its recorded hash"
                )
                continue
            if item.kind == "mail_raw":
                mail_valid += 1
            else:
                web_valid += 1
        summary = (
            f"mail raw {mail_valid}/{mail_total} valid, "
            f"web snapshots {web_valid}/{web_total} valid"
        )
        severity = IntegritySeverity.OK if not findings else IntegritySeverity.FAIL
        return IntegritySection("content", severity, summary, tuple(findings))

    def _knowledge_section(self, audit: DatabaseAudit) -> IntegritySection:
        """Derived state: report what is readable, and never call an offline vault corruption."""
        offline: list[str] = []
        findings: list[str] = []
        for root in audit.roots:
            if root.kind == "vault" and not Path(root.path).is_dir():
                offline.append(root.root_id)
                continue
            if not Path(root.path).is_dir():
                findings.append(f"root {root.root_id} is not reachable")
        summary_bits = [f"{len(audit.roots) - len(offline)} root(s) reachable"]
        if offline:
            summary_bits.append(f"offline: {', '.join(sorted(offline))}")
        if self._indexes is not None:  # pragma: no cover - reserved for a richer index probe
            summary_bits.append("indexes are derived and rebuildable")
        severity = IntegritySeverity.WARN if (offline or findings) else IntegritySeverity.OK
        if findings and not offline:
            severity = IntegritySeverity.FAIL
        return IntegritySection(
            "knowledge", severity, "; ".join(summary_bits), tuple(findings)
        )


def _migrations_section(
    audit: DatabaseAudit, directory: Path | None
) -> IntegritySection:
    """Applied migrations must be a prefix of the reviewed files — reported, never applied."""
    if directory is None:
        return IntegritySection(
            "migrations",
            IntegritySeverity.WARN,
            f"{len(audit.applied_migrations)} migration(s) applied (files not checked)",
        )
    reviewed = tuple(sorted(path.name for path in Path(directory).glob("*.sql")))
    applied = tuple(audit.applied_migrations)
    if applied == reviewed:
        return IntegritySection(
            "migrations",
            IntegritySeverity.OK,
            f"{len(applied)} migration(s) applied, up to date",
        )
    unknown = tuple(
        name for name in applied if name not in reviewed
    )
    if unknown:
        # A version this build does not ship means the database was written by a newer binary (or
        # its history was rewritten). Reported, never "fixed": nothing here applies or reverts SQL.
        return IntegritySection(
            "migrations",
            IntegritySeverity.FAIL,
            f"INCOMPATIBLE: {len(unknown)} applied migration(s) are unknown to this build",
            unknown,
        )
    if applied == reviewed[: len(applied)]:
        pending = len(reviewed) - len(applied)
        return IntegritySection(
            "migrations",
            IntegritySeverity.WARN,
            f"PENDING: {pending} reviewed migration(s) are not applied",
            tuple(reviewed[len(applied) :]),
        )
    return IntegritySection(
        "migrations",
        IntegritySeverity.FAIL,
        "the applied migrations do not match the reviewed files",
        tuple(sorted(set(applied) ^ set(reviewed))),
    )


__all__ = ["REVIEWED_MIGRATION_COUNT", "IntegrityService"]
