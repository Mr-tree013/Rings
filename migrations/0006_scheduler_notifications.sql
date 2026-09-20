-- 0006_scheduler_notifications.sql — durable scheduling intent and the notification inbox
-- (Phase 3C, ADR-0016).
--
-- Process state is ephemeral; scheduling intent is durable. A `scheduled_job` says "at this
-- instant, this fixed kind of work becomes due" and lives in the runtime database, so a
-- daemon restart recovers it instead of losing it. Execution is at-least-once and fenced by a
-- lease claim token; the unique active dedup index keeps "one logical reminder" and "one
-- pending rolling replan" from multiplying.
--
-- `notifications` is the durable inbox reminders are delivered into; its UNIQUE dedup key is
-- what makes a reminder idempotent across a crash between "notification written" and "job
-- completed".
CREATE TABLE scheduled_jobs (
    id               TEXT PRIMARY KEY,
    kind             TEXT NOT NULL,
    status           TEXT NOT NULL,

    due_at           TEXT NOT NULL,
    next_attempt_at  TEXT NULL,

    dedup_key        TEXT NOT NULL,
    payload_json     TEXT NOT NULL,

    attempts         INTEGER NOT NULL,
    last_error       TEXT NULL,

    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    completed_at     TEXT NULL,
    cancelled_at     TEXT NULL,

    claim_token      TEXT NULL,
    claimed_by       TEXT NULL,
    claimed_at       TEXT NULL,
    lease_expires_at TEXT NULL,

    CONSTRAINT scheduled_jobs_kind_is_known
        CHECK (kind IN ('deadline_reminder', 'rolling_replan')),
    CONSTRAINT scheduled_jobs_status_is_known
        CHECK (status IN ('pending', 'processing', 'completed', 'cancelled', 'dead_lettered')),
    CONSTRAINT scheduled_jobs_attempts_not_negative CHECK (attempts >= 0),
    CONSTRAINT scheduled_jobs_dedup_key_not_blank CHECK (length(trim(dedup_key)) > 0),
    -- Payloads are typed JSON data, never executable code: an object, at rest, always.
    CONSTRAINT scheduled_jobs_payload_is_object
        CHECK (json_valid(payload_json) AND json_type(payload_json) = 'object'),
    CONSTRAINT scheduled_jobs_lease_is_complete
        CHECK (
            (claim_token IS NULL AND claimed_by IS NULL AND claimed_at IS NULL
                AND lease_expires_at IS NULL)
            OR (claim_token IS NOT NULL AND claimed_by IS NOT NULL AND claimed_at IS NOT NULL
                AND lease_expires_at IS NOT NULL)
        ),
    CONSTRAINT scheduled_jobs_lease_matches_status
        CHECK ((status = 'processing') = (claim_token IS NOT NULL)),
    CONSTRAINT scheduled_jobs_terminal_timestamps_are_consistent
        CHECK (
            (status IN ('completed', 'dead_lettered')
                AND completed_at IS NOT NULL AND cancelled_at IS NULL)
            OR (status = 'cancelled' AND cancelled_at IS NOT NULL AND completed_at IS NULL)
            OR (status IN ('pending', 'processing')
                AND completed_at IS NULL AND cancelled_at IS NULL)
        )
);

-- At most one *active* job per dedup key. Terminal history may repeat: a cancelled reminder
-- is evidence, not noise.
CREATE UNIQUE INDEX scheduled_jobs_active_dedup_idx
    ON scheduled_jobs (dedup_key)
    WHERE status IN ('pending', 'processing');

CREATE INDEX scheduled_jobs_due_idx ON scheduled_jobs (status, due_at);
CREATE INDEX scheduled_jobs_effective_due_idx
    ON scheduled_jobs (status, COALESCE(next_attempt_at, due_at));
CREATE INDEX scheduled_jobs_lease_idx ON scheduled_jobs (status, lease_expires_at);

CREATE TABLE notifications (
    id                  TEXT PRIMARY KEY,
    kind                TEXT NOT NULL,
    status              TEXT NOT NULL,

    title               TEXT NOT NULL,
    body                TEXT NOT NULL,

    related_task_id     TEXT NULL REFERENCES tasks (id),
    related_proposal_id TEXT NULL REFERENCES plan_proposals (id),

    dedup_key           TEXT NOT NULL UNIQUE,

    created_at          TEXT NOT NULL,
    read_at             TEXT NULL,

    CONSTRAINT notifications_kind_is_known
        CHECK (kind IN ('deadline_reminder', 'plan_ready', 'scheduler_warning')),
    CONSTRAINT notifications_status_is_known CHECK (status IN ('unread', 'read')),
    CONSTRAINT notifications_title_not_blank CHECK (length(trim(title)) > 0),
    CONSTRAINT notifications_body_not_blank CHECK (length(trim(body)) > 0),
    CONSTRAINT notifications_dedup_key_not_blank CHECK (length(trim(dedup_key)) > 0),
    CONSTRAINT notifications_read_consistency
        CHECK ((status = 'unread' AND read_at IS NULL) OR (status = 'read' AND read_at IS NOT NULL))
);

CREATE INDEX notifications_status_idx ON notifications (status, created_at, id);
