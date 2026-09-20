"""The read-only database audit behind `pw integrity check` (ADR-0031).

Every check in here answers a question a person would ask after a crash, a restore or a suspicious
week: does SQLite think the file is sound, do the foreign keys hold, does every stored action still
hash to its own fingerprint, does every approval still point at the action it was given for, is
there at most one current fact per key, does every cited source exist?

Three rules hold throughout:

- **read-only.** No pragma that writes, no migration, no repair, no `UPDATE`. A check that fixed
  something would destroy the evidence that something went wrong;
- **bounded and content-free.** Findings name entity kinds, counts and identifiers, never a mail
  body, a page excerpt, a fact value or an action payload. Fingerprints are re-derived from stored
  bytes and never printed;
- **honest severities.** A tampered payload or a broken approval link is `CRITICAL`; a missing
  content object is `FAIL`; an offline vault or a derived index that can be rebuilt is `WARN`.
"""

from __future__ import annotations

import hashlib
import sqlite3

from assistant.domain.integrity import IntegritySection, IntegritySeverity
from assistant.ports.integrity_repository import (
    ConfiguredRoot,
    DatabaseAudit,
    IntegrityObject,
)
from assistant.store.db import Database
from assistant.store.errors import StoreError

_OBSERVATION_EVENT_TYPES = ("web.page.changed", "manual.input.received")


class SqliteIntegrityRepository:
    """A read-only audit of one runtime database."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def audit(self) -> DatabaseAudit:
        """Run every read-only check and return the sections it produced."""
        try:
            with self._database.connect() as connection:
                connection.row_factory = sqlite3.Row
                sections = (
                    _database_section(connection),
                    _capability_section(connection),
                    _conversation_section(connection),
                    _learning_section(connection),
                    _mail_section(connection),
                    _observation_section(connection),
                )
                return DatabaseAudit(
                    sections=sections,
                    objects=_referenced_objects(connection),
                    roots=_configured_roots(connection),
                    applied_migrations=_applied_migrations(connection),
                )
        except sqlite3.Error as exc:
            raise StoreError(f"could not audit the runtime database: {exc}") from exc


def _database_section(connection: sqlite3.Connection) -> IntegritySection:
    integrity = [
        str(row[0])
        for row in connection.execute("PRAGMA integrity_check").fetchall()
        if str(row[0]).lower() != "ok"
    ]
    keys = [
        f"table {row['table']} rowid {row['rowid']}"
        for row in connection.execute("PRAGMA foreign_key_check").fetchall()
    ]
    if integrity:
        return IntegritySection(
            "database",
            IntegritySeverity.FAIL,
            "SQLite reports the file is not sound",
            tuple(integrity),
        )
    if keys:
        return IntegritySection(
            "database",
            IntegritySeverity.FAIL,
            "foreign keys do not hold",
            tuple(keys),
        )
    return IntegritySection("database", IntegritySeverity.OK, "integrity and foreign keys OK")


def _capability_section(connection: sqlite3.Connection) -> IntegritySection:
    """Approvals, challenges, executions and send links, cross-checked."""
    findings: list[str] = []
    critical: list[str] = []
    for row in connection.execute(
        "SELECT id, payload_json, fingerprint FROM action_requests ORDER BY id"
    ).fetchall():
        derived = hashlib.sha256(str(row["payload_json"]).encode("utf-8")).hexdigest()
        if derived != str(row["fingerprint"]):
            # The payload is never printed: the id and the fact are enough to act on.
            critical.append(f"action {row['id']} does not hash to its stored fingerprint")
    for row in connection.execute(
        "SELECT c.id AS challenge_id FROM approval_challenges AS c "
        "LEFT JOIN action_requests AS a ON a.id = c.action_id "
        "WHERE a.id IS NULL OR a.fingerprint <> c.action_fingerprint"
    ).fetchall():
        critical.append(f"challenge {row['challenge_id']} does not match its action")
    for row in connection.execute(
        "SELECT p.id AS approval_id FROM approvals AS p "
        "LEFT JOIN action_requests AS a ON a.id = p.action_id "
        "WHERE a.id IS NULL OR a.fingerprint <> p.action_fingerprint"
    ).fetchall():
        critical.append(f"approval {row['approval_id']} does not match its action fingerprint")
    for row in connection.execute(
        "SELECT r.id AS run_id FROM execution_runs AS r "
        "LEFT JOIN approvals AS p ON p.id = r.approval_id "
        "WHERE p.id IS NULL OR p.action_id <> r.action_id"
    ).fetchall():
        critical.append(f"execution run {row['run_id']} does not belong to its action")
    for row in connection.execute(
        "SELECT l.action_id AS action_id FROM mail_send_links AS l "
        "LEFT JOIN action_requests AS a ON a.id = l.action_id "
        "WHERE a.id IS NULL OR a.action_type <> 'mail.send'"
    ).fetchall():
        critical.append(f"mail send link {row['action_id']} is not a mail.send action")
    unresolved = connection.execute(
        "SELECT count(*) FROM execution_runs WHERE status IN ('running', 'unknown')"
    ).fetchone()[0]
    if critical:
        return IntegritySection(
            "capabilities",
            IntegritySeverity.CRITICAL,
            "stored approvals disagree with their actions",
            tuple(critical) + tuple(findings),
        )
    return IntegritySection(
        "capabilities",
        IntegritySeverity.OK,
        f"actions, approvals and executions OK ({unresolved} unresolved)",
    )


def _conversation_section(connection: sqlite3.Connection) -> IntegritySection:
    """A turn must stay inside its own thread, and an interrupted write must stay visible.

    The foreign keys already guarantee that a message and a turn exist; they cannot guarantee that
    they belong to the *same* conversation, which is the invariant the runtime reads by. An
    `UNKNOWN_LOCAL` operation is not corruption — it is the honest record of a write whose outcome
    nobody knows — but it is worth a warning, because only a human can settle it.
    """
    critical: list[str] = []
    for row in connection.execute(
        "SELECT t.id AS turn_id FROM conversation_turns AS t "
        "LEFT JOIN conversation_messages AS m ON m.id = t.user_message_id "
        "WHERE m.id IS NULL OR m.thread_id <> t.thread_id"
    ).fetchall():
        critical.append(f"conversation turn {row['turn_id']} does not own its user message")
    for row in connection.execute(
        "SELECT t.id AS turn_id FROM conversation_turns AS t "
        "LEFT JOIN conversation_messages AS m ON m.id = t.assistant_message_id "
        "WHERE t.assistant_message_id IS NOT NULL "
        "AND (m.id IS NULL OR m.thread_id <> t.thread_id)"
    ).fetchall():
        critical.append(f"conversation turn {row['turn_id']} does not own its assistant message")
    for row in connection.execute(
        "SELECT o.id AS operation_id FROM conversation_operations AS o "
        "LEFT JOIN conversation_turns AS t ON t.id = o.turn_id WHERE t.id IS NULL"
    ).fetchall():
        critical.append(f"conversation operation {row['operation_id']} has no turn")
    if critical:
        return IntegritySection(
            "conversation",
            IntegritySeverity.CRITICAL,
            "stored conversations disagree with their own history",
            tuple(critical),
        )
    unresolved = connection.execute(
        "SELECT COUNT(*) FROM conversation_operations WHERE status = 'unknown_local'"
    ).fetchone()[0]
    if unresolved:
        return IntegritySection(
            "conversation",
            IntegritySeverity.WARN,
            f"{unresolved} interrupted local write(s) may have completed; inspect them before "
            "retrying",
        )
    return IntegritySection(
        "conversation", IntegritySeverity.OK, "threads, turns and operations OK"
    )


def _learning_section(connection: sqlite3.Connection) -> IntegritySection:
    """Facts and playbooks: provenance, supersession and promotion links."""
    critical: list[str] = []
    for row in connection.execute(
        "SELECT c.id AS candidate_id FROM fact_candidates AS c "
        "LEFT JOIN corrections AS r ON r.id = c.correction_id WHERE r.id IS NULL"
    ).fetchall():
        critical.append(f"fact candidate {row['candidate_id']} has no source correction")
    for row in connection.execute(
        "SELECT f.id AS fact_id FROM confirmed_facts AS f "
        "LEFT JOIN fact_candidates AS c ON c.id = f.candidate_id "
        "WHERE c.id IS NULL OR c.fact_key <> f.fact_key OR c.value <> f.value"
    ).fetchall():
        critical.append(
            f"confirmed fact {row['fact_id']} no longer matches its candidate snapshot"
        )
    for row in connection.execute(
        "SELECT fact_key FROM confirmed_facts WHERE superseded_at IS NULL "
        "GROUP BY fact_key HAVING count(*) > 1"
    ).fetchall():
        critical.append(f"fact key {row['fact_key']} has more than one current fact")
    for row in connection.execute(
        "SELECT p.id AS playbook_id FROM playbooks AS p "
        "LEFT JOIN playbook_candidates AS c ON c.id = p.candidate_id "
        "LEFT JOIN playbook_replay_tests AS t ON t.id = p.promoted_from_test_id "
        "WHERE c.id IS NULL OR t.id IS NULL OR t.candidate_id <> p.candidate_id"
    ).fetchall():
        critical.append(f"playbook {row['playbook_id']} has broken promotion provenance")
    for row in connection.execute(
        "SELECT c.id AS candidate_id FROM playbook_candidates AS c "
        "LEFT JOIN action_requests AS a ON a.id = c.source_action_id "
        "LEFT JOIN execution_runs AS r ON r.id = c.source_execution_run_id "
        "WHERE a.id IS NULL OR r.id IS NULL OR r.action_id <> c.source_action_id"
    ).fetchall():
        critical.append(
            f"playbook candidate {row['candidate_id']} cites a source that does not match"
        )
    if critical:
        return IntegritySection(
            "learning",
            IntegritySeverity.CRITICAL,
            "stored learning disagrees with itself",
            tuple(critical),
        )
    return IntegritySection("learning", IntegritySeverity.OK, "facts and playbooks OK")


def _mail_section(connection: sqlite3.Connection) -> IntegritySection:
    """Mail identity, threading, drafts and send links."""
    findings: list[str] = []
    # A message without a thread membership is normal: threading is derived and happens when the
    # message is analyzed. What must hold is that every membership points at something real, that a
    # `linked` member has its parent in the same thread, and that drafts and send links resolve.
    for row in connection.execute(
        "SELECT t.message_id AS message_id FROM mail_thread_members AS t "
        "LEFT JOIN mail_threads AS r ON r.id = t.thread_id "
        "WHERE r.id IS NULL"
    ).fetchall():
        findings.append(f"thread membership {row['message_id']} points at a missing thread")
    for row in connection.execute(
        "SELECT t.message_id AS message_id FROM mail_thread_members AS t "
        "LEFT JOIN mail_thread_members AS p ON p.message_id = t.parent_message_id "
        "WHERE t.link_status = 'linked' "
        "AND (p.message_id IS NULL OR p.thread_id <> t.thread_id)"
    ).fetchall():
        findings.append(f"message {row['message_id']} links to a parent in another thread")
    for row in connection.execute(
        "SELECT l.mail_message_id AS message_id FROM mail_event_links AS l "
        "LEFT JOIN inbound_events AS e ON e.id = l.inbound_event_id WHERE e.id IS NULL"
    ).fetchall():
        findings.append(f"mail event link {row['message_id']} points at a missing event")
    for row in connection.execute(
        "SELECT d.id AS draft_id FROM mail_drafts AS d "
        "LEFT JOIN mail_messages AS m ON m.id = d.reply_to_message_id WHERE m.id IS NULL"
    ).fetchall():
        findings.append(f"draft {row['draft_id']} replies to a missing message")
    for row in connection.execute(
        "SELECT l.action_id AS action_id FROM mail_send_links AS l "
        "LEFT JOIN mail_drafts AS d ON d.id = l.draft_id WHERE d.id IS NULL"
    ).fetchall():
        findings.append(f"send link {row['action_id']} points at a missing draft")
    if findings:
        return IntegritySection(
            "mail",
            IntegritySeverity.FAIL,
            "stored mail relationships are broken",
            tuple(findings),
        )
    return IntegritySection("mail", IntegritySeverity.OK, "messages, threads and drafts OK")


def _observation_section(connection: sqlite3.Connection) -> IntegritySection:
    """Web observation versioning, bridges and analyses."""
    critical: list[str] = []
    for row in connection.execute(
        "SELECT o.id AS observation_id FROM web_observations AS o "
        "LEFT JOIN web_observations AS p ON p.id = o.previous_observation_id "
        "WHERE (o.is_baseline = 1 AND o.previous_observation_id IS NOT NULL) "
        "OR (o.is_baseline = 0 AND (o.previous_observation_id IS NULL OR p.id IS NULL))"
    ).fetchall():
        critical.append(
            f"web observation {row['observation_id']} has an inconsistent predecessor"
        )
    for row in connection.execute(
        "SELECT l.observation_id AS observation_id FROM web_observation_event_links AS l "
        "LEFT JOIN inbound_events AS e ON e.id = l.inbound_event_id "
        "WHERE e.id IS NULL OR e.event_type <> 'web.page.changed' "
        "OR e.external_id <> 'observation:' || l.observation_id"
    ).fetchall():
        critical.append(f"web event link {row['observation_id']} does not match its event")
    for row in connection.execute(
        "SELECT l.manual_input_id AS input_id FROM manual_input_event_links AS l "
        "LEFT JOIN inbound_events AS e ON e.id = l.inbound_event_id "
        "WHERE e.id IS NULL OR e.event_type <> 'manual.input.received' "
        "OR e.external_id <> 'manual-input:' || l.manual_input_id"
    ).fetchall():
        critical.append(f"manual event link {row['input_id']} does not match its event")
    for row in connection.execute(
        "SELECT a.id AS analysis_id FROM observation_analyses AS a "
        "LEFT JOIN inbound_events AS e ON e.id = a.inbound_event_id "
        f"WHERE e.id IS NULL OR e.event_type NOT IN {_OBSERVATION_EVENT_TYPES}"
    ).fetchall():
        critical.append(f"analysis {row['analysis_id']} does not belong to an observation event")
    if critical:
        return IntegritySection(
            "observations",
            IntegritySeverity.CRITICAL,
            "stored observations disagree with their events",
            tuple(critical),
        )
    return IntegritySection(
        "observations", IntegritySeverity.OK, "web and manual observations OK"
    )


def _referenced_objects(connection: sqlite3.Connection) -> tuple[IntegrityObject, ...]:
    """Every content object the database references, with the hash it recorded."""
    objects: list[IntegrityObject] = []
    for row in connection.execute(
        "SELECT DISTINCT raw_storage_key, raw_sha256 FROM mail_messages "
        "WHERE raw_storage_key IS NOT NULL AND raw_sha256 IS NOT NULL "
        "ORDER BY raw_storage_key"
    ).fetchall():
        objects.append(
            IntegrityObject(
                kind="mail_raw",
                storage_key=str(row["raw_storage_key"]),
                sha256=str(row["raw_sha256"]),
            )
        )
    for row in connection.execute(
        "SELECT DISTINCT storage_key, content_sha256 FROM web_observations "
        "ORDER BY storage_key"
    ).fetchall():
        objects.append(
            IntegrityObject(
                kind="web_snapshot",
                storage_key=str(row["storage_key"]),
                sha256=str(row["content_sha256"]),
            )
        )
    return tuple(objects)


def _configured_roots(connection: sqlite3.Connection) -> tuple[ConfiguredRoot, ...]:
    return tuple(
        ConfiguredRoot(
            root_id=str(row["root_id"]),
            kind=str(row["kind"]),
            path=str(row["last_known_path"]),
            reachable=str(row["last_known_path"]) != "",
        )
        for row in connection.execute(
            "SELECT root_id, kind, last_known_path FROM storage_roots ORDER BY root_id"
        ).fetchall()
    )


def _applied_migrations(connection: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM schema_migrations ORDER BY version"
        ).fetchall()
    )


__all__ = ["SqliteIntegrityRepository"]
