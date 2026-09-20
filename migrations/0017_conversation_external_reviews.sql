-- 0017_conversation_external_reviews.sql — conversational review of one exact external action
-- (Phase 10B, ADR-0034).
--
-- A review is a *pointer to a decision*, never a copy of one. It remembers which conversation
-- operation prepared which immutable `ActionRequest`, what that action's fingerprint was, and how
-- the human settled it — so the exact payload can be re-rendered from the action itself, and a
-- confirmation can be refused the moment the two stop agreeing.
--
-- Deliberately absent: any challenge plaintext, any approval token, any SMTP credential, any raw
-- body copy and any model reasoning. The approval itself stays where it has always been — in
-- `approvals`, created by the existing service at the moment the user confirms, never before.

CREATE TABLE conversation_external_reviews (
    id                       TEXT PRIMARY KEY,
    conversation_operation_id TEXT NOT NULL REFERENCES conversation_operations (id),
    thread_id                TEXT NOT NULL REFERENCES conversation_threads (id),

    action_request_id        TEXT NOT NULL REFERENCES action_requests (id),
    action_type              TEXT NOT NULL,
    action_fingerprint       TEXT NOT NULL,

    status                   TEXT NOT NULL,
    expires_at               TEXT NOT NULL,
    execution_run_id         TEXT NULL,

    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,

    -- Phase 10B is mail-only: the review table cannot describe another external capability,
    -- because no other capability has a conversational review designed for it.
    CONSTRAINT conversation_external_reviews_action_type_is_reviewed
        CHECK (action_type = 'mail.send'),
    CONSTRAINT conversation_external_reviews_status_is_known
        CHECK (status IN ('waiting', 'cancelled', 'expired', 'stale', 'approved', 'succeeded',
                          'failed', 'unknown')),
    CONSTRAINT conversation_external_reviews_fingerprint_is_sha256
        CHECK (length(action_fingerprint) = 64
               AND action_fingerprint NOT GLOB '*[^0-9a-f]*'),
    CONSTRAINT conversation_external_reviews_expiry_is_after_creation
        CHECK (expires_at > created_at),
    CONSTRAINT conversation_external_reviews_execution_consistency
        CHECK (
            -- Succeeded and unknown can only come from an execution attempt, so they must name the
            -- run. Failed is the one state an attempt can also reach *without* a run (the
            -- capability was unavailable, so nothing was ever sent), so it stays optional.
            (status IN ('succeeded', 'unknown') AND execution_run_id IS NOT NULL)
            OR (status IN ('waiting', 'cancelled', 'expired', 'stale', 'approved')
                AND execution_run_id IS NULL)
            OR status = 'failed'
        )
);

-- At most one live review per conversation thread: one thread, one thing waiting for a human.
CREATE UNIQUE INDEX conversation_external_reviews_thread_waiting_idx
    ON conversation_external_reviews (thread_id)
    WHERE status = 'waiting';

-- At most one live review per conversation operation: a turn cannot queue two sends.
CREATE UNIQUE INDEX conversation_external_reviews_operation_idx
    ON conversation_external_reviews (conversation_operation_id);

CREATE INDEX conversation_external_reviews_action_idx
    ON conversation_external_reviews (action_request_id);

CREATE INDEX conversation_external_reviews_status_idx
    ON conversation_external_reviews (status);
