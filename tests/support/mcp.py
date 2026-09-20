"""Helpers for the Phase 8B tests: a real facade, real services and a buildable server.

The MCP tests exercise the *real* application surface over real SQLite: the facade is composed the
way the composition root composes it, and the server is built with the same registration functions
the console script uses. Nothing is mocked except the model (which the MCP surface must never
touch) and the clock.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from assistant.adapters.mcp.server import build_server
from assistant.adapters.web_watch.snapshot_store import WebSnapshotStore  # noqa: F401
from assistant.application.mcp_facade import McpFacade
from assistant.domain.config import AssistantConfig, McpConfig, PlanningConfig, ReminderConfig
from assistant.store.db import Database
from tests.support.fakes import FakeClock

NOW = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)
SERVER_VERSION = "0.8.0"

MAIL_BODY_SENTINEL = "MAIL-BODY-SENTINEL-DO-NOT-LEAK"
DRAFT_BODY_SENTINEL = "DRAFT-BODY-SENTINEL-DO-NOT-LEAK"
FACT_VALUE_SENTINEL = "FACT-VALUE-SENTINEL-DO-NOT-LEAK"
PLAYBOOK_NOTE_SENTINEL = "PLAYBOOK-NOTE-SENTINEL-DO-NOT-LEAK"
APPROVAL_TOKEN_SENTINEL = "APPROVAL-TOKEN-SENTINEL-DO-NOT-LEAK"
EHALL_PAYLOAD_SENTINEL = "EHALL-PAYLOAD-SENTINEL-DO-NOT-LEAK"
KNOWLEDGE_SENTINEL = "knowledge-sentinel-alpha"

SENSITIVE_SENTINELS: tuple[str, ...] = (
    MAIL_BODY_SENTINEL,
    DRAFT_BODY_SENTINEL,
    FACT_VALUE_SENTINEL,
    PLAYBOOK_NOTE_SENTINEL,
    APPROVAL_TOKEN_SENTINEL,
    EHALL_PAYLOAD_SENTINEL,
)
"""Strings that must never appear on the default MCP surface."""


def config_for(
    *,
    write_scope: str = "none",
    expose_knowledge: bool = False,
    planning: bool = False,
    reminders: bool = True,
) -> AssistantConfig:
    """A host configuration for one MCP capability mode."""
    return AssistantConfig(
        planning=PlanningConfig(timezone="Asia/Shanghai") if planning else None,
        reminders=ReminderConfig() if reminders else ReminderConfig(deadline_offsets_minutes=()),
        mcp=McpConfig(
            enabled=True, write_scope=write_scope, expose_knowledge=expose_knowledge
        ),
    )


class StubKnowledge:
    """A scripted local search: the MCP surface must call this and nothing else."""

    def __init__(self, *, hits: int = 1, calls: list[str] | None = None) -> None:
        self.hits = hits
        self.calls = calls if calls is not None else []

    async def search_context(self, query: str, *, root_id: str | None = None, limit: int = 8):
        """Return bounded synthetic hits and record the query it was given."""
        from assistant.domain.knowledge import (
            KnowledgeContextHit,
            KnowledgeContextSearchResult,
            SourceSpan,
            SourceSpanKind,
        )

        self.calls.append(query)
        content = f"{KNOWLEDGE_SENTINEL} " + ("x" * 4000)
        return KnowledgeContextSearchResult(
            query=query,
            content_hits=tuple(
                KnowledgeContextHit(
                    root_id="university",
                    entry_id=uuid4(),
                    chunk_id=uuid4(),
                    ordinal=index,
                    logical_uri="local://university/notes/alpha.md",
                    source_span=SourceSpan(
                        kind=SourceSpanKind.LINE, line_start=1, line_end=3
                    ),
                    content=content,
                    score=1.0,
                )
                for index in range(self.hits)
            ),
            metadata_hits=(),
            offline_roots=("offline-root",),
            identity_mismatches=(),
        )


def seed_sentinels(database: Database) -> None:
    """Write one row per sensitive store, so a leak through the MCP surface would be visible.

    Rows are inserted in dependency order, because every one of them is real: the mail message the
    draft replies to, the case the action lives in, the approval and run the playbook candidate
    cites. A surface that leaked any of them would be leaking actual stored content.
    """
    stamp = NOW.isoformat(timespec="microseconds")
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO mail_messages (id, account_id, message_id_header, references_json, "
            "to_addresses_json, cc_addresses_json, reply_to_addresses_json, body_status, "
            "content_fingerprint, size_bytes, parse_warnings, first_seen_at, last_seen_at) "
            "VALUES ('11111111-1111-1111-1111-111111111111', 'smail', '<a@example.edu>', "
            "'[]', '[]', '[]', '[]', 'available', ?, 10, 0, ?, ?)",
            ("a" * 64, stamp, stamp),
        )
        connection.execute(
            "INSERT INTO mail_drafts (id, account_id, reply_to_message_id, to_addresses_json, "
            "subject, body_text, needs_user_input_json, origin, version, "
            "generation_input_fingerprint, prompt_version, created_at, updated_at) "
            "VALUES ('22222222-2222-2222-2222-222222222222', 'smail', "
            "'11111111-1111-1111-1111-111111111111', ?, 'Re: x', ?, "
            "'[]', 'model_generated', 1, ?, 1, ?, ?)",
            (
                json.dumps(["ada@example.edu"]),
                DRAFT_BODY_SENTINEL,
                "b" * 64,
                stamp,
                stamp,
            ),
        )
        connection.execute(
            "INSERT INTO corrections (id, text, created_at) VALUES (?, ?, ?)",
            ("33333333-3333-3333-3333-333333333333", FACT_VALUE_SENTINEL, stamp),
        )
        connection.execute(
            "INSERT INTO fact_candidates (id, fact_key, value, correction_id, status, "
            "proposed_valid_until, created_at, resolved_at) VALUES (?, 'profile.office', ?, ?, "
            "'pending', NULL, ?, NULL)",
            (
                "44444444-4444-4444-4444-444444444444",
                FACT_VALUE_SENTINEL,
                "33333333-3333-3333-3333-333333333333",
                stamp,
            ),
        )
        connection.execute(
            "INSERT INTO cases (id, title, status, created_at, updated_at, completed_at, "
            "cancelled_at) VALUES ('99999999-9999-9999-9999-999999999999', 'A case', 'open', ?, "
            "?, NULL, NULL)",
            (stamp, stamp),
        )
        connection.execute(
            "INSERT INTO action_requests (id, case_id, action_type, payload_json, fingerprint, "
            "status, created_at, executed_at, cancelled_at) VALUES "
            "('88888888-8888-8888-8888-888888888888', "
            "'99999999-9999-9999-9999-999999999999', 'ehall.submit-certificate', ?, ?, "
            "'executed', ?, ?, NULL)",
            (json.dumps({"note": EHALL_PAYLOAD_SENTINEL}), "d" * 64, stamp, stamp),
        )
        connection.execute(
            "INSERT INTO approvals (id, action_id, action_fingerprint, approved_at, expires_at, "
            "consumed_at, superseded_at) VALUES "
            "('cccccccc-cccc-cccc-cccc-cccccccccccc', "
            "'88888888-8888-8888-8888-888888888888', ?, ?, ?, ?, NULL)",
            (
                "d" * 64,
                stamp,
                (NOW + timedelta(minutes=10)).isoformat(timespec="microseconds"),
                stamp,
            ),
        )
        connection.execute(
            "INSERT INTO execution_runs (id, action_id, approval_id, status, started_at, "
            "finished_at, error_summary) VALUES "
            "('77777777-7777-7777-7777-777777777777', "
            "'88888888-8888-8888-8888-888888888888', "
            "'cccccccc-cccc-cccc-cccc-cccccccccccc', 'succeeded', ?, ?, NULL)",
            (stamp, stamp),
        )
        connection.execute(
            "INSERT INTO playbook_candidates (id, name, note, source_action_id, "
            "source_execution_run_id, source_action_type, source_action_fingerprint, status, "
            "created_at, resolved_at) VALUES (?, 'candidate', ?, ?, ?, "
            "'ehall.submit-certificate', ?, 'pending', ?, NULL)",
            (
                "55555555-5555-5555-5555-555555555555",
                PLAYBOOK_NOTE_SENTINEL,
                "88888888-8888-8888-8888-888888888888",
                "77777777-7777-7777-7777-777777777777",
                "d" * 64,
                stamp,
            ),
        )
        connection.execute(
            "INSERT INTO approval_challenges (id, action_id, action_fingerprint, token_hash, "
            "created_at, expires_at, consumed_at) VALUES "
            "('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', "
            "'88888888-8888-8888-8888-888888888888', ?, ?, ?, ?, NULL)",
            (
                "d" * 64,
                "e" * 64,
                stamp,
                (NOW + timedelta(minutes=10)).isoformat(timespec="microseconds"),
            ),
        )
        connection.execute(
            "INSERT INTO mail_send_links (action_id, draft_id, draft_version, rfc_message_id, "
            "created_at) VALUES (?, ?, 1, '<x@example.edu>', ?)",
            (
                "88888888-8888-8888-8888-888888888888",
                "22222222-2222-2222-2222-222222222222",
                stamp,
            ),
        )
        connection.execute(
            "INSERT INTO web_observations (id, target_id, url, content_sha256, storage_key, "
            "previous_observation_id, is_baseline, fetched_at) VALUES (?, 'course-notices', "
            "'https://example.edu/notices', ?, 'web/snapshots/aa/aaa.txt', NULL, 1, ?)",
            ("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "f" * 64, stamp),
        )


@dataclass
class McpHarness:
    """A composed MCP surface over one real database."""

    database: Database
    clock: FakeClock = field(default_factory=lambda: FakeClock(start=NOW))
    snapshots: Path | None = None

    def facade(self, config: AssistantConfig, *, knowledge: object | None = None) -> McpFacade:
        """The facade the composition root would build for this configuration."""
        from assistant import bootstrap

        composed = bootstrap.mcp_facade(config, clock=self.clock, database=self.database)
        if knowledge is None:
            return composed
        return McpFacade(
            composed._tasks,
            composed._cases,
            composed._commitments,
            composed._planning,
            composed._notifications,
            self.clock,
            version=SERVER_VERSION,
            write_scope=config.mcp.write_scope,
            expose_knowledge=config.mcp.expose_knowledge,
            knowledge=knowledge,  # type: ignore[arg-type]
        )

    def server(self, config: AssistantConfig, *, knowledge: object | None = None) -> object:
        """A real server instance with the surface this configuration allows."""
        return build_server(
            self.facade(config, knowledge=knowledge), version=SERVER_VERSION
        )


__all__ = [
    "APPROVAL_TOKEN_SENTINEL",
    "DRAFT_BODY_SENTINEL",
    "EHALL_PAYLOAD_SENTINEL",
    "FACT_VALUE_SENTINEL",
    "KNOWLEDGE_SENTINEL",
    "MAIL_BODY_SENTINEL",
    "NOW",
    "PLAYBOOK_NOTE_SENTINEL",
    "SENSITIVE_SENTINELS",
    "SERVER_VERSION",
    "McpHarness",
    "StubKnowledge",
    "config_for",
    "seed_sentinels",
]
