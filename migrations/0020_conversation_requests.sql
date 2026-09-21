-- 0020_conversation_requests.sql — the durable accepted-input queue (Phase 11A, ADR-0041).
--
-- A browser submit is a *request*, not a message: it is accepted, queued, claimed by a worker and
-- only then handed to the existing conversation runtime. Two properties of this table are the
-- whole reason it exists:
--
--   UNIQUE(thread_id, client_request_id)   a POST retry settles on the same accepted row, so a
--                                          browser timeout can never turn into a second turn
--   one PROCESSING row per thread          the conversational order of one thread is preserved,
--                                          while different threads stay independent
--
-- `input_text` is stored while the intent must survive a restart and cleared once the durable
-- conversation message exists and the request is terminal, so the user's words are not kept twice.
--
-- What is deliberately absent: model prompts, provider responses, hidden reasoning, approval
-- challenges, and any trace of the ephemeral SSE stream. The event channel is not state.

CREATE TABLE conversation_requests (
    id                TEXT PRIMARY KEY,
    thread_id         TEXT NOT NULL REFERENCES conversation_threads (id),
    client_request_id TEXT NOT NULL,

    input_text TEXT NULL,

    status TEXT NOT NULL,
    stage  TEXT NULL,

    turn_id    TEXT NULL REFERENCES conversation_turns (id),
    error_code TEXT NULL,

    cancel_requested_at TEXT NULL,

    created_at  TEXT NOT NULL,
    started_at  TEXT NULL,
    finished_at TEXT NULL,

    CONSTRAINT conversation_requests_status_is_known
        CHECK (status IN ('queued', 'processing', 'completed', 'failed', 'cancelled',
                          'interrupted')),
    -- The stage vocabulary is the *safe UI* one: coarse application phases, never a step of
    -- anybody's reasoning. A row that names anything else is refused by the database.
    CONSTRAINT conversation_requests_stage_is_known
        CHECK (stage IS NULL OR stage IN ('queued', 'understanding', 'reading_local_state',
                                          'planning', 'querying_knowledge', 'preparing_mail',
                                          'updating_local_state', 'waiting_confirmation',
                                          'external_execution', 'finalizing')),
    CONSTRAINT conversation_requests_client_id_is_bounded
        CHECK (length(client_request_id) BETWEEN 8 AND 128),
    CONSTRAINT conversation_requests_input_is_bounded
        CHECK (input_text IS NULL
               OR (length(trim(input_text)) > 0 AND length(input_text) <= 4000)),
    -- A queued request has not been handed to a worker, so it has no start time. Everything else
    -- that a worker touched, or that the user cancelled after claiming, does.
    CONSTRAINT conversation_requests_started_consistency
        CHECK (status IN ('queued', 'cancelled') OR started_at IS NOT NULL),
    CONSTRAINT conversation_requests_queued_has_no_start
        CHECK (status <> 'queued' OR started_at IS NULL),
    -- Terminal state and the moment it became terminal are the same fact.
    CONSTRAINT conversation_requests_finished_consistency
        CHECK ((status IN ('completed', 'failed', 'cancelled', 'interrupted'))
               = (finished_at IS NOT NULL)),
    CONSTRAINT conversation_requests_times_are_ordered
        CHECK (started_at IS NULL OR finished_at IS NULL OR finished_at >= started_at),
    -- An error code is a bounded product-level code, never a rendered exception.
    CONSTRAINT conversation_requests_error_code_is_bounded
        CHECK (error_code IS NULL OR (length(error_code) BETWEEN 1 AND 64))
);

-- The idempotency key. A browser retry of the same POST lands on this row, whatever it costs.
CREATE UNIQUE INDEX conversation_requests_thread_client_idx
    ON conversation_requests (thread_id, client_request_id);

-- The worker's query: oldest queued work for one thread, and the per-thread FIFO order.
CREATE INDEX conversation_requests_thread_status_idx
    ON conversation_requests (thread_id, status, created_at, id);

-- The restart scan and the integrity check: everything in one status, oldest first.
CREATE INDEX conversation_requests_status_idx
    ON conversation_requests (status, created_at, id);

-- One active request per thread, enforced by the database rather than by a worker's discipline.
CREATE UNIQUE INDEX conversation_requests_one_active_idx
    ON conversation_requests (thread_id)
    WHERE status = 'processing';

-- Crash correlation: which turn this request produced. A turn has at most one request, and the
-- index is partial so the historical turns written before Phase 11A keep their NULL.
ALTER TABLE conversation_turns ADD COLUMN request_id TEXT NULL
    REFERENCES conversation_requests (id);

CREATE UNIQUE INDEX conversation_turns_request_idx
    ON conversation_turns (request_id)
    WHERE request_id IS NOT NULL;
