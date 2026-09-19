-- 0001_initial.sql — inbound event storage (Phase 1A).
--
-- The database, not Python, owns the idempotency guarantee: (source, external_id) is
-- unique whenever external_id is present, so fetching the same mail twice cannot create
-- a second record even if two processes race.
CREATE TABLE inbound_events (
    id          TEXT    PRIMARY KEY,
    source      TEXT    NOT NULL,
    external_id TEXT    NULL,
    event_type  TEXT    NOT NULL,
    content     TEXT    NULL,
    received_at TEXT    NOT NULL,
    status      TEXT    NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT    NULL,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    CONSTRAINT inbound_events_status_is_known
        CHECK (status IN ('RECEIVED', 'PROCESSING', 'PROCESSED', 'FAILED')),
    CONSTRAINT inbound_events_attempts_not_negative
        CHECK (attempts >= 0),
    CONSTRAINT inbound_events_source_not_blank
        CHECK (length(trim(source)) > 0),
    CONSTRAINT inbound_events_type_not_blank
        CHECK (length(trim(event_type)) > 0),
    CONSTRAINT inbound_events_external_id_not_blank
        CHECK (external_id IS NULL OR length(trim(external_id)) > 0),
    CONSTRAINT inbound_events_failed_carries_error
        CHECK (status <> 'FAILED' OR (last_error IS NOT NULL AND length(trim(last_error)) > 0))
);

CREATE UNIQUE INDEX inbound_events_source_external_id_key
    ON inbound_events (source, external_id)
    WHERE external_id IS NOT NULL;

CREATE INDEX inbound_events_status_received_at_idx
    ON inbound_events (status, received_at);

