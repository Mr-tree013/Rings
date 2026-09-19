-- 0004_commitment_core.sql — durable commitment state (Phase 3A, ADR-0014).
--
-- Five concepts, five tables. A task is not a calendar event, a deadline is not a time block,
-- and a plan is not a record of work; collapsing any pair would make the planner's job
-- ambiguous later.
--
-- Times are stored as fixed-width UTC ISO 8601 text (see store/serialization.py), so the
-- `ends_at > starts_at` checks below are valid: they compare the same canonical format.
CREATE TABLE tasks (
    id                TEXT PRIMARY KEY,
    title             TEXT NOT NULL,
    description       TEXT NULL,
    status            TEXT NOT NULL,
    priority          TEXT NOT NULL,
    estimated_minutes INTEGER NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    completed_at      TEXT NULL,
    cancelled_at      TEXT NULL,
    CONSTRAINT tasks_status_is_known
        CHECK (status IN ('open', 'completed', 'cancelled')),
    CONSTRAINT tasks_priority_is_known
        CHECK (priority IN ('low', 'normal', 'high')),
    CONSTRAINT tasks_title_not_blank
        CHECK (length(trim(title)) > 0),
    CONSTRAINT tasks_estimate_at_least_one_minute
        CHECK (estimated_minutes IS NULL OR estimated_minutes >= 1),
    CONSTRAINT tasks_open_has_no_terminal_time
        CHECK (status <> 'open' OR (completed_at IS NULL AND cancelled_at IS NULL)),
    CONSTRAINT tasks_completed_consistency
        CHECK (status <> 'completed' OR (completed_at IS NOT NULL AND cancelled_at IS NULL)),
    CONSTRAINT tasks_cancelled_consistency
        CHECK (status <> 'cancelled' OR (cancelled_at IS NOT NULL AND completed_at IS NULL))
);

-- One task has at most one active deadline. Removal is an explicit application operation
-- (`clear_deadline`), and there is no physical delete API for business objects in this phase.
CREATE TABLE deadlines (
    id         TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL UNIQUE REFERENCES tasks (id),
    due_at     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE calendar_events (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    description  TEXT NULL,
    starts_at    TEXT NOT NULL,
    ends_at      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    cancelled_at TEXT NULL,
    CONSTRAINT calendar_events_title_not_blank
        CHECK (length(trim(title)) > 0),
    CONSTRAINT calendar_events_positive_duration
        CHECK (ends_at > starts_at)
);

CREATE TABLE plan_blocks (
    id           TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL REFERENCES tasks (id),
    starts_at    TEXT NOT NULL,
    ends_at      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    cancelled_at TEXT NULL,
    CONSTRAINT plan_blocks_positive_duration
        CHECK (ends_at > starts_at)
);

-- Overlap between blocks is intentionally *not* a database constraint: a planner (or a human)
-- may plan overlapping time on purpose, so overlap policy lives in the application.
CREATE INDEX plan_blocks_task_idx ON plan_blocks (task_id);
CREATE INDEX plan_blocks_starts_at_idx ON plan_blocks (starts_at);
CREATE INDEX plan_blocks_ends_at_idx ON plan_blocks (ends_at);

CREATE TABLE work_sessions (
    id         TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL REFERENCES tasks (id),
    started_at TEXT NOT NULL,
    ended_at   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CONSTRAINT work_sessions_positive_duration
        CHECK (ended_at > started_at)
);

-- Durations are never stored: `duration_seconds` is derived from the two timestamps, so the
-- record cannot drift from the facts.
CREATE INDEX work_sessions_task_idx ON work_sessions (task_id);
CREATE INDEX work_sessions_started_at_idx ON work_sessions (started_at);

