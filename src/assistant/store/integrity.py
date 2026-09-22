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
import json
import sqlite3

from assistant.domain.contact import ContactStatus
from assistant.domain.errors import DomainError
from assistant.domain.integrity import IntegritySection, IntegritySeverity
from assistant.domain.mail_send import MailSendKind, MailSendPayload
from assistant.ports.integrity_repository import (
    ConfiguredRoot,
    DatabaseAudit,
    IntegrityObject,
)
from assistant.store.contacts import CONTACT_FIELDS, row_to_contact
from assistant.store.db import Database
from assistant.store.errors import StoreError
from assistant.store.recurring_calendar import RULE_FIELDS, row_to_rule

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
                    _recurring_section(connection),
                    _contact_section(connection),
                    _mail_section(connection),
                    _outbound_section(connection),
                    _attention_section(connection),
                    _planning_preferences_section(connection),
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
    # Conversational external reviews (Phase 10B): a review is a pointer to one exact action, so
    # every link in that chain has to still hold.
    for row in connection.execute(
        "SELECT r.id AS review_id FROM conversation_external_reviews AS r "
        "LEFT JOIN conversation_operations AS o ON o.id = r.conversation_operation_id "
        "LEFT JOIN conversation_turns AS t ON t.id = o.turn_id "
        "WHERE o.id IS NULL OR t.id IS NULL OR t.thread_id <> r.thread_id"
    ).fetchall():
        critical.append(f"conversation review {row['review_id']} does not belong to its thread")
    for row in connection.execute(
        "SELECT r.id AS review_id FROM conversation_external_reviews AS r "
        "LEFT JOIN action_requests AS a ON a.id = r.action_request_id "
        "WHERE a.id IS NULL OR a.action_type <> r.action_type "
        "OR a.fingerprint <> r.action_fingerprint"
    ).fetchall():
        critical.append(f"conversation review {row['review_id']} does not match its action")
    for row in connection.execute(
        "SELECT r.id AS review_id FROM conversation_external_reviews AS r "
        "LEFT JOIN execution_runs AS e ON e.id = r.execution_run_id "
        "WHERE r.execution_run_id IS NOT NULL "
        "AND (e.id IS NULL OR e.action_id <> r.action_request_id)"
    ).fetchall():
        critical.append(f"conversation review {row['review_id']} names another action's run")
    # The durable accepted-input queue (Phase 11A). These are read-only checks over the queue's own
    # invariants: a request belongs to a real thread, a correlated turn belongs to the same thread
    # and to that request alone, no thread has two active requests, and no terminal row still claims
    # to be live. Message text is deliberately never inspected.
    for row in connection.execute(
        "SELECT r.id AS request_id FROM conversation_requests AS r "
        "LEFT JOIN conversation_threads AS t ON t.id = r.thread_id WHERE t.id IS NULL"
    ).fetchall():
        critical.append(f"conversation request {row['request_id']} has no thread")
    for row in connection.execute(
        "SELECT r.id AS request_id FROM conversation_requests AS r "
        "LEFT JOIN conversation_turns AS t ON t.id = r.turn_id "
        "WHERE r.turn_id IS NOT NULL AND (t.id IS NULL OR t.thread_id <> r.thread_id)"
    ).fetchall():
        critical.append(
            f"conversation request {row['request_id']} names a turn of another thread"
        )
    for row in connection.execute(
        "SELECT t.id AS turn_id FROM conversation_turns AS t "
        "LEFT JOIN conversation_requests AS r ON r.id = t.request_id "
        "WHERE t.request_id IS NOT NULL AND r.id IS NULL"
    ).fetchall():
        critical.append(f"conversation turn {row['turn_id']} names a missing request")
    for row in connection.execute(
        "SELECT thread_id FROM conversation_requests WHERE status = 'processing' "
        "GROUP BY thread_id HAVING COUNT(*) > 1"
    ).fetchall():
        critical.append(f"thread {row['thread_id']} has more than one active request")
    for row in connection.execute(
        "SELECT id, status, stage, started_at, finished_at FROM conversation_requests"
    ).fetchall():
        status = str(row["status"])
        started = row["started_at"]
        finished = row["finished_at"]
        terminal = status in ("completed", "failed", "cancelled", "interrupted")
        if status not in (
            "queued",
            "processing",
            "completed",
            "failed",
            "cancelled",
            "interrupted",
        ):
            critical.append(f"conversation request {row['id']} has an unknown status")
        if terminal != (finished is not None):
            critical.append(
                f"conversation request {row['id']} disagrees about being finished"
            )
        if status == "queued" and started is not None:
            critical.append(f"conversation request {row['id']} started while queued")
        if status == "processing" and (started is None or finished is not None):
            critical.append(
                f"conversation request {row['id']} is processing without a live start"
            )
        if status == "processing" and row["stage"] is None:
            critical.append(f"conversation request {row['id']} is active with no stage")
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


def _recurring_section(connection: sqlite3.Connection) -> IntegritySection:
    """Weekly commitments: every rule must still mean what it says it means (ADR-0036 §3).

    Each rule is read back through the domain constructor — so its weekday, local time order,
    IANA zone, date order and retirement consistency are all re-checked — and its fingerprint is
    re-derived from the stored fields. Occurrences are never materialised, so there is nothing
    derived here to be stale.
    """
    critical: list[str] = []
    failing: list[str] = []
    total = 0
    active = 0
    for row in connection.execute(
        f"SELECT {RULE_FIELDS} FROM recurring_calendar_rules ORDER BY created_at, id"
    ).fetchall():
        total += 1
        identifier = str(row[0])[:8]
        stored_fingerprint = str(row[9])
        try:
            rule = row_to_rule(row)
        except (DomainError, ValueError, KeyError) as exc:
            failing.append(f"weekly rule {identifier} cannot be interpreted: {exc}")
            continue
        if rule.status.value == "active":
            active += 1
        if rule.fingerprint != stored_fingerprint:
            # Stored authority disagreeing with itself, exactly like a rewritten action payload.
            critical.append(
                f"weekly rule {identifier} does not hash to its stored fingerprint"
            )
    for row in connection.execute(
        "SELECT rule_fingerprint AS fingerprint, count(*) AS total "
        "FROM recurring_calendar_rules WHERE status = 'active' "
        "GROUP BY rule_fingerprint HAVING count(*) > 1"
    ).fetchall():
        critical.append(
            f"{row['total']} active weekly rules share one meaning "
            f"({str(row['fingerprint'])[:8]})"
        )
    summary = f"{total} weekly rule(s) checked, {active} active"
    if critical:
        return IntegritySection("recurring", IntegritySeverity.CRITICAL, summary, tuple(critical))
    if failing:
        return IntegritySection("recurring", IntegritySeverity.FAIL, summary, tuple(failing))
    return IntegritySection("recurring", IntegritySeverity.OK, summary)


def _contact_section(connection: sqlite3.Connection) -> IntegritySection:
    """Contacts: every live record must still mean the name and address it claims (ADR-0037 §35).

    Each row is read back through the domain constructor — so its name, address, lifecycle and
    timestamps are re-checked — and its fingerprint is re-derived from the stored fields. A
    fingerprint is an identity, never content, so nothing here prints an address.
    """
    critical: list[str] = []
    failing: list[str] = []
    total = 0
    active = 0
    for row in connection.execute(
        f"SELECT {CONTACT_FIELDS} FROM contacts ORDER BY created_at, id"
    ).fetchall():
        total += 1
        identifier = str(row[0])[:8]
        try:
            contact = row_to_contact(row)
        except (DomainError, ValueError, KeyError) as exc:
            failing.append(f"contact {identifier} cannot be interpreted: {exc}")
            continue
        if contact.status is ContactStatus.ACTIVE:
            active += 1
        if contact.fingerprint != str(row[5]):
            critical.append(f"contact {identifier} does not hash to its stored fingerprint")
        if contact.email_key != str(row[3]):
            critical.append(f"contact {identifier} address key does not match its address")
    for row in connection.execute(
        "SELECT contact_fingerprint AS fingerprint, count(*) AS total FROM contacts "
        "WHERE status = 'active' GROUP BY contact_fingerprint HAVING count(*) > 1"
    ).fetchall():
        critical.append(
            f"{row['total']} active contacts share one name and address "
            f"({str(row['fingerprint'])[:8]})"
        )
    summary = f"{total} contact(s) checked, {active} active"
    if critical:
        return IntegritySection("contacts", IntegritySeverity.CRITICAL, summary, tuple(critical))
    if failing:
        return IntegritySection("contacts", IntegritySeverity.FAIL, summary, tuple(failing))
    return IntegritySection("contacts", IntegritySeverity.OK, summary)


def _outbound_section(connection: sqlite3.Connection) -> IntegritySection:
    """New outbound mail: the link and the approved payload must describe the same letter.

    A `mail.send` link names either a reply draft or a new-mail draft; for the new-mail half this
    re-derives the payload and checks that its kind, draft id and draft version are exactly the
    ones the link recorded — the invariant that makes "what was reviewed is what is sent" a fact
    about stored data rather than a hope (ADR-0037 §35).
    """
    critical: list[str] = []
    failing: list[str] = []
    total = 0
    for row in connection.execute(
        "SELECT l.action_id, l.new_draft_id, l.draft_version, a.payload_json "
        "FROM mail_send_links AS l "
        "LEFT JOIN action_requests AS a ON a.id = l.action_id "
        "WHERE l.new_draft_id IS NOT NULL ORDER BY l.created_at, l.action_id"
    ).fetchall():
        total += 1
        identifier = str(row["action_id"])[:8]
        if row["payload_json"] is None:
            critical.append(f"new mail send {identifier} has no action")
            continue
        try:
            payload = MailSendPayload.from_payload(json.loads(str(row["payload_json"])))
        except (DomainError, ValueError, TypeError) as exc:
            failing.append(f"new mail send {identifier} has an unreadable payload: {exc}")
            continue
        if payload.kind is not MailSendKind.NEW:
            critical.append(f"new mail send {identifier} is not tagged as a new letter")
        if str(payload.draft_id) != str(row["new_draft_id"]):
            critical.append(f"new mail send {identifier} names a different draft than its link")
        if payload.draft_version != int(str(row["draft_version"])):
            critical.append(f"new mail send {identifier} names a different draft version")
    for row in connection.execute(
        "SELECT l.action_id FROM mail_send_links AS l "
        "LEFT JOIN new_mail_drafts AS d ON d.id = l.new_draft_id "
        "WHERE l.new_draft_id IS NOT NULL AND d.version < l.draft_version"
    ).fetchall():
        critical.append(
            f"new mail send {str(row['action_id'])[:8]} snapshots a version the draft never had"
        )
    summary = f"{total} new mail send(s) checked"
    if critical:
        return IntegritySection("outbound", IntegritySeverity.CRITICAL, summary, tuple(critical))
    if failing:
        return IntegritySection("outbound", IntegritySeverity.FAIL, summary, tuple(failing))
    return IntegritySection("outbound", IntegritySeverity.OK, summary)


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
        "LEFT JOIN mail_drafts AS d ON d.id = l.draft_id "
        "WHERE l.draft_id IS NOT NULL AND d.id IS NULL"
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


def _attention_section(connection: sqlite3.Connection) -> IntegritySection:
    """Attention: identity, lifecycle timestamps and source plausibility (ADR-0042 §72).

    The checks are about *derived state agreeing with itself*. An attention row that claims to be
    acknowledged without an acknowledgement moment, or that carries a fingerprint which is not a
    SHA-256 digest, is a row no reader could trust — and one the projector will happily keep
    updating, which is exactly why an audit has to look.
    """
    critical: list[str] = []
    for row in connection.execute(
        "SELECT id, status, acknowledged_at, dismissed_at, resolved_at, "
        "source_fingerprint, generation, length(trim(dedupe_key)) AS key_length "
        "FROM attention_items ORDER BY id"
    ).fetchall():
        identifier = row["id"]
        status = str(row["status"])
        stamps = (row["acknowledged_at"], row["dismissed_at"], row["resolved_at"])
        expected = {
            "open": (None, None, None),
            "acknowledged": (stamps[0], None, None),
            "dismissed": (None, stamps[1], None),
            "resolved": (None, None, stamps[2]),
        }[status]
        disowned = any(
            expected[index] is None and stamps[index] is not None for index in range(3)
        )
        if disowned:
            critical.append(f"attention {identifier} carries a stamp its {status} state disowns")
        elif status != "open" and all(stamp is None for stamp in stamps):
            critical.append(f"attention {identifier} is {status} with no moment to show for it")
        fingerprint = str(row["source_fingerprint"])
        if len(fingerprint) != 64 or any(char not in "0123456789abcdef" for char in fingerprint):
            critical.append(f"attention {identifier} does not carry a SHA-256 fingerprint")
        if int(row["generation"]) < 1:
            critical.append(f"attention {identifier} has a generation below one")
        if int(row["key_length"]) < 1:
            critical.append(f"attention {identifier} has a blank dedupe key")
    for row in connection.execute(
        "SELECT dedupe_key FROM attention_items WHERE status <> 'resolved' "
        "GROUP BY dedupe_key HAVING COUNT(*) > 1"
    ).fetchall():
        critical.append(f"dedupe key {row['dedupe_key']} is live more than once")
    if critical:
        return IntegritySection(
            "attention",
            IntegritySeverity.CRITICAL,
            "derived attention rows disagree with their own lifecycle",
            tuple(critical),
        )
    return IntegritySection("attention", IntegritySeverity.OK, "attention rows OK")


def _planning_preferences_section(connection: sqlite3.Connection) -> IntegritySection:
    """Planning preferences and plan-block supersession (ADR-0044 §72).

    Preferences are user policy: a set that describes an impossible day would silently plan nothing,
    so it is a `FAIL` rather than a warning. Supersession is `CRITICAL` when half-recorded, because
    a block that names a replacing proposal without a moment (or the reverse) is an audit trail that
    cannot be read.
    """
    findings: list[str] = []
    warning = False
    for row in connection.execute(
        "SELECT id, day_start_local, day_end_local, max_daily_minutes, "
        "preferred_block_minutes, max_block_minutes FROM planning_preferences"
    ).fetchall():
        identifier = row["id"]
        start, end = int(row["day_start_local"]), int(row["day_end_local"])
        daily = int(row["max_daily_minutes"])
        preferred, maximum = int(row["preferred_block_minutes"]), int(row["max_block_minutes"])
        if not (0 <= start < end <= 1440):
            findings.append(f"planning preferences {identifier} describe an impossible day")
        if not 1 <= daily <= 1440:
            findings.append(f"planning preferences {identifier} have a daily limit outside a day")
        if not 1 <= preferred <= maximum <= 1440:
            findings.append(f"planning preferences {identifier} have inconsistent block lengths")
    for row in connection.execute(
        "SELECT id FROM plan_blocks "
        "WHERE (superseded_at IS NULL) <> (superseded_by_proposal_id IS NULL)"
    ).fetchall():
        findings.append(f"plan block {row['id']} records half of a supersession")
    for row in connection.execute(
        "SELECT b.id AS block_id FROM plan_blocks AS b "
        "LEFT JOIN plan_proposals AS p ON p.id = b.superseded_by_proposal_id "
        "WHERE b.superseded_by_proposal_id IS NOT NULL AND p.id IS NULL"
    ).fetchall():
        findings.append(f"plan block {row['block_id']} names a proposal that is not stored")
    for row in connection.execute(
        "SELECT id FROM plan_blocks "
        "WHERE superseded_at IS NOT NULL AND cancelled_at IS NULL"
    ).fetchall():
        findings.append(f"plan block {row['id']} is superseded but still current")
    if findings:
        return IntegritySection(
            "planning",
            IntegritySeverity.FAIL,
            "planning preferences or plan-block history do not hold",
            tuple(findings),
        )
    if warning:  # pragma: no cover - reserved for a future softer finding
        return IntegritySection("planning", IntegritySeverity.WARN, "planning state is unusual")
    return IntegritySection("planning", IntegritySeverity.OK, "planning preferences and history OK")


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
