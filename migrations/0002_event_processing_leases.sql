-- 0002_event_processing_leases.sql — durable worker metadata (Phase 1C, ADR-0010).
--
-- 0001's status CHECK predates DEAD_LETTERED and SQLite cannot alter a CHECK constraint,
-- so the table is rebuilt: new table -> copy rows -> drop old -> rename -> recreate
-- indexes. The migration runner wraps this whole file in one transaction, so a failure
-- leaves the 0001 schema and the existing rows untouched.
--
-- Claim metadata is all-or-nothing per row: either a row is leased (token, owner,
-- claimed_at, lease expiry all present) or it is not. That invariant is enforced here so a
-- half-written lease can never be mistaken for a live one.
CREATE TABLE inbound_events_new (
    id               TEXT    PRIMARY KEY,
    source           TEXT    NOT NULL,
    external_id      TEXT    NULL,
    event_type       TEXT    NOT NULL,
    content          TEXT    NULL,
    received_at      TEXT    NOT NULL,
    status           TEXT    NOT NULL,
    attempts         INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT    NULL,
    next_attempt_at  TEXT    NULL,
    claim_token      TEXT    NULL,
    claimed_by       TEXT    NULL,
    claimed_at       TEXT    NULL,
    lease_expires_at TEXT    NULL,
    dead_lettered_at TEXT    NULL,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL,
    CONSTRAINT inbound_events_status_is_known
        CHECK (status IN ('RECEIVED', 'PROCESSING', 'PROCESSED', 'FAILED', 'DEAD_LETTERED')),
    CONSTRAINT inbound_events_attempts_not_negative
        CHECK (attempts >= 0),
    CONSTRAINT inbound_events_source_not_blank
        CHECK (length(trim(source)) > 0),
    CONSTRAINT inbound_events_type_not_blank
        CHECK (length(trim(event_type)) > 0),
    CONSTRAINT inbound_events_external_id_not_blank
        CHECK (external_id IS NULL OR length(trim(external_id)) > 0),
    CONSTRAINT inbound_events_failure_carries_error
        CHECK (status NOT IN ('FAILED', 'DEAD_LETTERED')
               OR (last_error IS NOT NULL AND length(trim(last_error)) > 0)),
    CONSTRAINT inbound_events_dead_letter_carries_timestamp
        CHECK (status <> 'DEAD_LETTERED' OR dead_lettered_at IS NOT NULL),
    CONSTRAINT inbound_events_lease_is_complete
        CHECK ((claim_token IS NULL
                AND claimed_by IS NULL
                AND claimed_at IS NULL
                AND lease_expires_at IS NULL)
            OR (claim_token IS NOT NULL
                AND claimed_by IS NOT NULL
                AND claimed_at IS NOT NULL
                AND lease_expires_at IS NOT NULL))
);

INSERT INTO inbound_events_new (
    id, source, external_id, event_type, content, received_at, status, attempts,
    last_error, created_at, updated_at
)
SELECT
    id, source, external_id, event_type, content, received_at, status, attempts,
    last_error, created_at, updated_at
FROM inbound_events;

DROP TABLE inbound_events;

ALTER TABLE inbound_events_new RENAME TO inbound_events;

-- Idempotency authority from 0001: unchanged, deliberately rebuilt identically.
CREATE UNIQUE INDEX inbound_events_source_external_id_key
    ON inbound_events (source, external_id)
    WHERE external_id IS NOT NULL;

CREATE INDEX inbound_events_status_received_at_idx
    ON inbound_events (status, received_at);

-- Claim eligibility always filters on status plus a deadline: the retry time for FAILED
-- rows and the lease expiry for abandoned PROCESSING rows.
CREATE INDEX inbound_events_status_deadlines_idx
    ON inbound_events (status, next_attempt_at, lease_expires_at);

