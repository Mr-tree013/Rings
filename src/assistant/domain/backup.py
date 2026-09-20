"""The operational backup format: one manifest, one database, referenced objects (ADR-0031).

```text
backup.gab
  manifest.json        format version, hashes, counts — never content
  runtime.sqlite3      a consistent snapshot of the runtime authority
  mail/raw/...         raw objects the snapshot references
  web/snapshots/...    normalized page text the snapshot references
```

Four properties are the whole safety story, and each one is a value here rather than a promise:

- **the archive has a fixed shape.** Only those four top-level names are allowed, so a member that
  looks like a knowledge index, an editor profile, a config file or a credential has nowhere to go;
- **every member name is checked, not trusted.** Absolute paths, `..` traversal, backslash
  traversal, drive letters, empty segments and NUL bytes are refused before anything is opened, and
  the verifier refuses duplicates, unlisted objects and missing ones;
- **the manifest is canonical.** Field order is fixed, JSON is sorted and compact, hashes are
  lowercase SHA-256, and parsing is strict — an unknown key, a wrong type or an unsupported format
  version is a refusal rather than a default;
- **the manifest carries identity, not content.** Storage keys, hashes, sizes and counts; no mail
  body, no page text, no action payload, no credential, no physical path of an original document.

The bounds are explicit and finite. A backup of a personal mail archive may legitimately be large,
so the total is generous, but it is a number: an archive that declares more members, a bigger
manifest, a bigger database or objects beyond those limits is refused rather than expanded.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime

from assistant.domain.errors import InvalidBackupArchive

FORMAT_VERSION = 1
"""The archive format this build writes and the only one it reads."""

ARCHIVE_SUFFIX = ".gab"

MANIFEST_MEMBER = "manifest.json"
DATABASE_MEMBER = "runtime.sqlite3"
MAIL_MEMBER_PREFIX = "mail/raw/"
WEB_MEMBER_PREFIX = "web/snapshots/"

ALLOWED_TOP_LEVEL: tuple[str, ...] = (
    MANIFEST_MEMBER,
    DATABASE_MEMBER,
    "mail",
    "web",
)
"""The only top-level names an archive may contain."""

DATABASE_FILENAME = "assistant.db"
"""The runtime database's file name inside a restored runtime directory (ADR-0003)."""

MAIL_ROOT_DIRECTORY = "mail"
"""Where the raw mail store lives under the runtime directory, matching the composition root."""

MAX_MEMBERS = 100_000
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_DATABASE_BYTES = 16 * 1024**3
MAX_OBJECT_BYTES = 64 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 64 * 1024**3
MAX_COMPRESSION_RATIO = 200
"""Declared uncompressed size divided by the archive's own size, as a cheap zip-bomb guard."""

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

_MANIFEST_KEYS: frozenset[str] = frozenset(
    {
        "application_version",
        "counts",
        "created_at",
        "database_member",
        "database_sha256",
        "format_version",
        "mail_objects",
        "migration_files",
        "web_snapshots",
    }
)
"""Exactly the keys a manifest carries: adding one is a format change, not a local edit."""
_DRIVE_LETTER_PATTERN = re.compile(r"^[A-Za-z]:")
_MAIL_MEMBER_PATTERN = re.compile(r"^mail/raw/([0-9a-f]{2})/([0-9a-f]{64})\.eml$")
_WEB_MEMBER_PATTERN = re.compile(r"^web/snapshots/([0-9a-f]{2})/([0-9a-f]{64})\.txt$")


def sha256_hex(payload: bytes) -> str:
    """The lowercase SHA-256 of some bytes. Never Python's `hash()`."""
    return hashlib.sha256(payload).hexdigest()


def canonical_json(document: object) -> str:
    """Serialize a document the one way this project compares and stores JSON."""
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def is_safe_member_name(name: str) -> bool:
    """Whether an archive member name is one this project is willing to write to disk.

    Refused: empty names, absolute paths, `..` traversal in either separator, backslashes, Windows
    drive letters, NUL bytes and empty path segments (`a//b`). The check is pure, so it runs before
    anything is extracted and can be tested on its own.
    """
    if not name or not name.strip():
        return False
    if "\x00" in name:
        return False
    if name.startswith("/") or name.startswith("\\"):
        return False
    if _DRIVE_LETTER_PATTERN.match(name):
        return False
    if "\\" in name:
        return False
    segments = name.split("/")
    return not any(segment in ("", ".", "..") for segment in segments)


def is_allowed_member(name: str) -> bool:
    """Whether a member belongs to the format at all.

    A content object must be named like the content address it is:
    `<prefix>/<first two hex>/<sha digest>.<suffix>`. That is what makes "an unexpected file" a
    name-level refusal rather than something a later check has to catch, so a `.bak`, a `.tmp` or a
    hand-added file has no way into an archive.
    """
    if not is_safe_member_name(name):
        return False
    if name in (MANIFEST_MEMBER, DATABASE_MEMBER):
        return True
    for pattern in (_MAIL_MEMBER_PATTERN, _WEB_MEMBER_PATTERN):
        match = pattern.match(name)
        if match is not None:
            return match.group(1) == match.group(2)[:2]
    return False


@dataclass(frozen=True, slots=True)
class RestoreFinalization:
    """What a restore invalidated, without naming a single token.

    A recovery retires capability, not history: approvals are superseded (they were never
    consumed), challenges and pairing codes are consumed (their only meaning is single use) and
    sessions are revoked. Nothing is deleted, and no `ExecutionRun` changes.
    """

    challenges_invalidated: int = 0
    approvals_superseded: int = 0
    pairing_tokens_invalidated: int = 0
    sessions_revoked: int = 0

    @property
    def total(self) -> int:
        """How many approval capabilities were retired."""
        return (
            self.challenges_invalidated
            + self.approvals_superseded
            + self.pairing_tokens_invalidated
            + self.sessions_revoked
        )


@dataclass(frozen=True, slots=True)
class BackupObject:
    """One content object in an archive: where it lives, what it hashes to, how big it is."""

    storage_key: str
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        if not is_allowed_member(self.storage_key) or self.storage_key in (
            MANIFEST_MEMBER,
            DATABASE_MEMBER,
        ):
            raise InvalidBackupArchive(f"unexpected archive member {self.storage_key!r}")
        if not SHA256_PATTERN.match(self.sha256):
            raise InvalidBackupArchive(
                f"object {self.storage_key!r} needs a lowercase SHA-256"
            )
        if not isinstance(self.size_bytes, int) or isinstance(self.size_bytes, bool):
            raise InvalidBackupArchive(f"object {self.storage_key!r} needs a byte size")
        if self.size_bytes < 0 or self.size_bytes > MAX_OBJECT_BYTES:
            raise InvalidBackupArchive(
                f"object {self.storage_key!r} declares {self.size_bytes} bytes, "
                f"outside the 0..{MAX_OBJECT_BYTES} range"
            )

    def to_document(self) -> dict[str, object]:
        """The manifest representation of this object."""
        return {
            "storage_key": self.storage_key,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_document(cls, document: object) -> BackupObject:
        """Rebuild one object from a manifest, refusing anything unexpected."""
        if not isinstance(document, dict) or set(document) != {
            "storage_key",
            "sha256",
            "size_bytes",
        }:
            raise InvalidBackupArchive("a manifest object must carry key, hash and size")
        key = document["storage_key"]
        digest = document["sha256"]
        size = document["size_bytes"]
        if not isinstance(key, str) or not isinstance(digest, str):
            raise InvalidBackupArchive("a manifest object needs string key and hash")
        if not isinstance(size, int) or isinstance(size, bool):
            raise InvalidBackupArchive("a manifest object needs an integer size")
        return cls(storage_key=key, sha256=digest, size_bytes=size)


@dataclass(frozen=True, slots=True)
class BackupCounts:
    """Aggregate counts, so an operator can recognise a backup without opening the database."""

    tasks: int = 0
    cases: int = 0
    actions: int = 0
    approvals: int = 0
    executions: int = 0
    mail_messages: int = 0
    mail_drafts: int = 0
    web_observations: int = 0
    manual_inputs: int = 0
    fact_candidates: int = 0
    confirmed_facts: int = 0
    playbook_candidates: int = 0
    playbooks: int = 0
    inbound_events: int = 0

    def to_document(self) -> dict[str, int]:
        """The manifest representation of the counts."""
        return {
            "actions": self.actions,
            "approvals": self.approvals,
            "cases": self.cases,
            "confirmed_facts": self.confirmed_facts,
            "executions": self.executions,
            "fact_candidates": self.fact_candidates,
            "inbound_events": self.inbound_events,
            "mail_drafts": self.mail_drafts,
            "mail_messages": self.mail_messages,
            "manual_inputs": self.manual_inputs,
            "playbook_candidates": self.playbook_candidates,
            "playbooks": self.playbooks,
            "tasks": self.tasks,
            "web_observations": self.web_observations,
        }

    @classmethod
    def from_document(cls, document: object) -> BackupCounts:
        """Rebuild the counts, refusing a manifest with a different key set."""
        if not isinstance(document, dict):
            raise InvalidBackupArchive("manifest counts must be an object")
        expected = set(cls().to_document())
        if set(document) != expected:
            raise InvalidBackupArchive(
                f"manifest counts have the wrong keys: {sorted(set(document) ^ expected)}"
            )
        values: dict[str, int] = {}
        for key, value in document.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise InvalidBackupArchive(f"manifest count {key!r} must be a non-negative int")
            values[key] = value
        return cls(**values)


@dataclass(frozen=True, slots=True)
class BackupManifest:
    """The identity of one archive: versions, hashes and counts."""

    application_version: str
    created_at: datetime
    database_sha256: str
    migration_files: tuple[str, ...]
    mail_objects: tuple[BackupObject, ...] = ()
    web_snapshots: tuple[BackupObject, ...] = ()
    counts: BackupCounts = BackupCounts()
    format_version: int = FORMAT_VERSION

    def __post_init__(self) -> None:
        if self.format_version != FORMAT_VERSION:
            raise InvalidBackupArchive(
                f"unsupported backup format version {self.format_version}"
            )
        if not self.application_version.strip():
            raise InvalidBackupArchive("a manifest needs the application version that wrote it")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise InvalidBackupArchive("created_at must be timezone-aware")
        if not SHA256_PATTERN.match(self.database_sha256):
            raise InvalidBackupArchive("the database hash must be lowercase SHA-256")
        if not self.migration_files:
            raise InvalidBackupArchive("a manifest must record the applied migrations")
        for name in self.migration_files:
            if not is_safe_member_name(name) or not name.endswith(".sql"):
                raise InvalidBackupArchive(f"migration file name {name!r} is not usable")
        keys = [item.storage_key for item in (*self.mail_objects, *self.web_snapshots)]
        if len(set(keys)) != len(keys):
            raise InvalidBackupArchive("a manifest must not list one object twice")

    def mail_keys(self) -> tuple[str, ...]:
        """The raw mail storage keys this archive carries."""
        return tuple(item.storage_key for item in self.mail_objects)

    def web_keys(self) -> tuple[str, ...]:
        """The web snapshot storage keys this archive carries."""
        return tuple(item.storage_key for item in self.web_snapshots)

    def object_for(self, member: str) -> BackupObject | None:
        """The object described for one member name, or `None` when it is not listed."""
        for item in (*self.mail_objects, *self.web_snapshots):
            if item.storage_key == member:
                return item
        return None

    def to_document(self) -> dict[str, object]:
        """The canonical document written into `manifest.json`."""
        return {
            "format_version": self.format_version,
            "application_version": self.application_version,
            "created_at": self.created_at.isoformat(),
            "database_member": DATABASE_MEMBER,
            "database_sha256": self.database_sha256,
            "migration_files": list(self.migration_files),
            "mail_objects": [item.to_document() for item in self.mail_objects],
            "web_snapshots": [item.to_document() for item in self.web_snapshots],
            "counts": self.counts.to_document(),
        }

    def to_json(self) -> str:
        """The canonical JSON of this manifest."""
        return canonical_json(self.to_document())

    @classmethod
    def from_json(cls, raw: bytes | str) -> BackupManifest:
        """Parse a manifest strictly, refusing a different shape or version.

        Raises:
            InvalidBackupArchive: the manifest is not JSON, has unexpected keys, a wrong type or an
                unsupported format version.
        """
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise InvalidBackupArchive(f"the manifest is not JSON: {exc.msg}") from exc
        if not isinstance(document, dict):
            raise InvalidBackupArchive("the manifest must be a JSON object")
        expected = set(_MANIFEST_KEYS)
        if set(document) != expected:
            raise InvalidBackupArchive(
                f"the manifest has the wrong keys: {sorted(set(document) ^ expected)}"
            )
        version = document["format_version"]
        if version != FORMAT_VERSION:
            raise InvalidBackupArchive(f"unsupported backup format version {version!r}")
        member = document["database_member"]
        if member != DATABASE_MEMBER:
            raise InvalidBackupArchive(f"unexpected database member {member!r}")
        created = document["created_at"]
        if not isinstance(created, str):
            raise InvalidBackupArchive("created_at must be an ISO 8601 string")
        try:
            created_at = datetime.fromisoformat(created)
        except ValueError as exc:
            raise InvalidBackupArchive("created_at is not ISO 8601") from exc
        migrations = document["migration_files"]
        if not isinstance(migrations, list) or not all(
            isinstance(item, str) for item in migrations
        ):
            raise InvalidBackupArchive("migration_files must be a list of names")
        mail = document["mail_objects"]
        web = document["web_snapshots"]
        if not isinstance(mail, list) or not isinstance(web, list):
            raise InvalidBackupArchive("mail_objects and web_snapshots must be lists")
        for entries, prefix, section in (
            (mail, MAIL_MEMBER_PREFIX, "mail_objects"),
            (web, WEB_MEMBER_PREFIX, "web_snapshots"),
        ):
            for item in entries:
                key = item.get("storage_key") if isinstance(item, dict) else None
                if not isinstance(key, str) or not key.startswith(prefix):
                    raise InvalidBackupArchive(
                        f"{section} contains {key!r}, which is not a {prefix} object"
                    )
        application_version = document["application_version"]
        database_sha256 = document["database_sha256"]
        if not isinstance(application_version, str) or not isinstance(database_sha256, str):
            raise InvalidBackupArchive("the manifest needs string version and database hash")
        return cls(
            application_version=application_version,
            created_at=created_at,
            database_sha256=database_sha256,
            migration_files=tuple(migrations),
            mail_objects=tuple(BackupObject.from_document(item) for item in mail),
            web_snapshots=tuple(BackupObject.from_document(item) for item in web),
            counts=BackupCounts.from_document(document["counts"]),
        )


__all__ = [
    "ALLOWED_TOP_LEVEL",
    "ARCHIVE_SUFFIX",
    "DATABASE_FILENAME",
    "DATABASE_MEMBER",
    "FORMAT_VERSION",
    "MAIL_MEMBER_PREFIX",
    "MAIL_ROOT_DIRECTORY",
    "MANIFEST_MEMBER",
    "MAX_COMPRESSION_RATIO",
    "MAX_DATABASE_BYTES",
    "MAX_MANIFEST_BYTES",
    "MAX_MEMBERS",
    "MAX_OBJECT_BYTES",
    "MAX_TOTAL_UNCOMPRESSED_BYTES",
    "WEB_MEMBER_PREFIX",
    "BackupCounts",
    "BackupManifest",
    "BackupObject",
    "RestoreFinalization",
    "canonical_json",
    "is_allowed_member",
    "is_safe_member_name",
    "sha256_hex",
]
