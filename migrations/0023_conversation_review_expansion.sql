-- 0023_conversation_review_expansion.sql — a second reviewed external capability (Phase 11E,
-- ADR-0045).
--
-- Phase 10B designed `conversation_external_reviews` for exactly one capability and said so in a
-- CHECK: `action_type = 'mail.send'`. Phase 11E reaches an already-reviewed second capability —
-- `ehall.submit-certificate`, prepared by `EHallCertificateService` and executed by the executor
-- that already exists — through the same review, the same exact-fingerprint approval and the same
-- execution service. So the constraint is widened from one value to a closed pair.
--
-- What this migration deliberately does *not* do:
--
--   * it does not make `action_type` arbitrary text — the allowed set stays closed, and the
--     application vocabulary (`ALLOWED_EXTERNAL_ACTION_TYPES`) is widened in the same change;
--   * it does not change a single column, so no historical mail review is rewritten, re-statused
--     or re-fingerprinted: every row is copied across exactly as it was;
--   * it does not add any column for credentials, cookies, page content, drafts or payload copies.
--     The review is still a pointer to one immutable `ActionRequest`.
--
-- SQLite cannot widen a table CHECK in place, so the table is rebuilt through a new one and
-- renamed, which is the same shape 0002, 0005 and 0019 already use.

CREATE TABLE conversation_external_reviews_rebuilt (
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

    -- Phase 11E widens the reviewed set from one capability to two, and no further: the two
    -- externally-effectful capabilities this build can perform, each of which has its own
    -- confirmation vocabulary, preview renderer and card kind. An action type that is not in this
    -- list has no conversational review, and naming it in the review table is a corrupt row.
    CONSTRAINT conversation_external_reviews_action_type_is_reviewed
        CHECK (action_type IN ('mail.send', 'ehall.submit-certificate')),
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
            (status IN ('succeeded', 'unknown') AND execution_run_id IS NOT NULL)
            OR (status IN ('waiting', 'cancelled', 'expired', 'stale', 'approved')
                AND execution_run_id IS NULL)
            OR status = 'failed'
        )
);

INSERT INTO conversation_external_reviews_rebuilt
    (id, conversation_operation_id, thread_id, action_request_id, action_type,
     action_fingerprint, status, expires_at, execution_run_id, created_at, updated_at)
SELECT
    id, conversation_operation_id, thread_id, action_request_id, action_type,
    action_fingerprint, status, expires_at, execution_run_id, created_at, updated_at
FROM conversation_external_reviews;

DROP TABLE conversation_external_reviews;

ALTER TABLE conversation_external_reviews_rebuilt RENAME TO conversation_external_reviews;

-- Unchanged from 0017, rebuilt deliberately: one thread waits for one thing, one operation queues
-- at most one review, and the integrity and recovery scans keep their indexes.
CREATE UNIQUE INDEX conversation_external_reviews_thread_waiting_idx
    ON conversation_external_reviews (thread_id)
    WHERE status = 'waiting';

CREATE UNIQUE INDEX conversation_external_reviews_operation_idx
    ON conversation_external_reviews (conversation_operation_id);

CREATE INDEX conversation_external_reviews_action_idx
    ON conversation_external_reviews (action_request_id);

CREATE INDEX conversation_external_reviews_status_idx
    ON conversation_external_reviews (status);
