-- 0016_conversations.sql — the Tree conversation runtime (Phase 10A, ADR-0033).
--
-- A conversation is durable history plus a durable record of what the system did with it. Three
-- tables carry that, and the separation is deliberate:
--
--   conversation_threads   one conversation, resumable across process restarts
--   conversation_messages  what was said, in order, by role
--   conversation_turns     what one user message was turned into (PLANNED / WAITING_CONFIRMATION /
--                          COMPLETED / FAILED / INTERRUPTED)
--   conversation_operations what the runtime was allowed to do about it, one row each
--
-- The operation row is the audit trail and the crash fence: `APPLYING` is written before a
-- mutating service is called, `APPLIED` only after it returned, and a process that dies in
-- between leaves `UNKNOWN_LOCAL`, which nothing ever replays automatically.
--
-- What is deliberately absent from this schema: raw model reasoning, chain-of-thought, prompt
-- text, and any approval token. A conversation cannot create an approval (ADR-0033 §15), so
-- there is nothing here to store one in.

CREATE TABLE conversation_threads (
    id          TEXT PRIMARY KEY,
    title       TEXT NULL,
    status      TEXT NOT NULL,

    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    archived_at TEXT NULL,

    CONSTRAINT conversation_threads_status_is_known
        CHECK (status IN ('active', 'archived')),
    CONSTRAINT conversation_threads_title_is_bounded
        CHECK (title IS NULL OR (length(trim(title)) > 0 AND length(title) <= 120)),
    CONSTRAINT conversation_threads_archived_consistency
        CHECK ((status = 'archived') = (archived_at IS NOT NULL))
);

CREATE TABLE conversation_messages (
    id         TEXT PRIMARY KEY,
    thread_id  TEXT NOT NULL REFERENCES conversation_threads (id),
    role       TEXT NOT NULL,
    text       TEXT NOT NULL,

    created_at TEXT NOT NULL,

    CONSTRAINT conversation_messages_role_is_known
        CHECK (role IN ('user', 'assistant')),
    CONSTRAINT conversation_messages_text_not_blank CHECK (length(trim(text)) > 0)
);

-- Messages are read in creation order inside one thread; the id is the deterministic tie-break.
CREATE INDEX conversation_messages_thread_idx
    ON conversation_messages (thread_id, created_at, id);

CREATE TABLE conversation_turns (
    id                  TEXT PRIMARY KEY,
    thread_id           TEXT NOT NULL REFERENCES conversation_threads (id),
    user_message_id     TEXT NOT NULL REFERENCES conversation_messages (id),
    assistant_message_id TEXT NULL REFERENCES conversation_messages (id),

    interpreter_version TEXT NOT NULL,
    context_fingerprint TEXT NOT NULL,

    status       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    completed_at TEXT NULL,

    CONSTRAINT conversation_turns_status_is_known
        CHECK (status IN ('planned', 'waiting_confirmation', 'completed', 'failed', 'interrupted')),
    CONSTRAINT conversation_turns_completion_consistency
        CHECK ((status IN ('completed', 'failed', 'interrupted')) = (completed_at IS NOT NULL))
);

CREATE INDEX conversation_turns_thread_idx ON conversation_turns (thread_id, created_at, id);
CREATE INDEX conversation_turns_pending_idx
    ON conversation_turns (thread_id, status)
    WHERE status = 'waiting_confirmation';

CREATE TABLE conversation_operations (
    id              TEXT PRIMARY KEY,
    turn_id         TEXT NOT NULL REFERENCES conversation_turns (id),
    ordinal         INTEGER NOT NULL,

    operation_type         TEXT NOT NULL,
    arguments_json         TEXT NOT NULL,
    operation_fingerprint  TEXT NOT NULL,

    status         TEXT NOT NULL,
    result_kind    TEXT NULL,
    result_ref     TEXT NULL,

    confirmation_expires_at TEXT NULL,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,

    CONSTRAINT conversation_operations_ordinal_not_negative CHECK (ordinal >= 0),
    CONSTRAINT conversation_operations_status_is_known
        CHECK (status IN ('proposed', 'waiting_confirmation', 'applying', 'applied',
                          'rejected', 'failed', 'unknown_local')),
    -- Arguments are typed JSON data, never executable code: an object, at rest, always.
    CONSTRAINT conversation_operations_arguments_is_object
        CHECK (json_valid(arguments_json) AND json_type(arguments_json) = 'object'),
    CONSTRAINT conversation_operations_fingerprint_is_sha256
        CHECK (length(operation_fingerprint) = 64
               AND operation_fingerprint NOT GLOB '*[^0-9a-f]*'),
    CONSTRAINT conversation_operations_confirmation_consistency
        CHECK (
            (status = 'waiting_confirmation' AND confirmation_expires_at IS NOT NULL)
            OR (status <> 'waiting_confirmation' AND confirmation_expires_at IS NULL)
        ),
    CONSTRAINT conversation_operations_result_consistency
        CHECK (
            (status = 'applied' AND result_kind IS NOT NULL)
            OR (status <> 'applied' AND result_kind IS NULL AND result_ref IS NULL)
        )
);

-- One ordinal per turn: the plan's order is part of what the user was shown.
CREATE UNIQUE INDEX conversation_operations_turn_ordinal_idx
    ON conversation_operations (turn_id, ordinal);

CREATE INDEX conversation_operations_status_idx ON conversation_operations (status);
