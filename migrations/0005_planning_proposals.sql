-- 0005_planning_proposals.sql — durable plan proposals and block provenance (Phase 3B, ADR-0015).
--
-- The planner never writes plan blocks directly: it produces a durable, reviewable proposal
-- (`plan_proposals` + `proposed_plan_blocks` + `planning_issues`) and only an explicit apply
-- turns it into real blocks. Plan blocks therefore need provenance: `manual` blocks belong to
-- the user and are never replaced by the planner, while `planner` blocks carry the proposal
-- that created them and are replaceable on the next apply.
--
-- `commitment_meta.revision` is the transactional fencing counter: every mutation that can
-- change a plan bumps it, and applying a proposal requires the revision it was built from.
CREATE TABLE plan_proposals (
    id                TEXT PRIMARY KEY,
    status            TEXT NOT NULL,
    window_start      TEXT NOT NULL,
    window_end        TEXT NOT NULL,
    timezone          TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    input_revision    INTEGER NOT NULL,
    created_at        TEXT NOT NULL,
    applied_at        TEXT NULL,
    superseded_at     TEXT NULL,
    CONSTRAINT plan_proposals_status_is_known
        CHECK (status IN ('pending', 'applied', 'superseded', 'stale')),
    CONSTRAINT plan_proposals_window_is_forward
        CHECK (window_end > window_start),
    CONSTRAINT plan_proposals_fingerprint_not_blank
        CHECK (length(trim(input_fingerprint)) > 0),
    CONSTRAINT plan_proposals_revision_not_negative
        CHECK (input_revision >= 0),
    CONSTRAINT plan_proposals_applied_consistency
        CHECK (status <> 'applied' OR applied_at IS NOT NULL),
    CONSTRAINT plan_proposals_superseded_consistency
        CHECK (status <> 'superseded' OR superseded_at IS NOT NULL)
);

CREATE TABLE proposed_plan_blocks (
    id          TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES plan_proposals (id),
    task_id     TEXT NOT NULL REFERENCES tasks (id),
    starts_at   TEXT NOT NULL,
    ends_at     TEXT NOT NULL,
    ordinal     INTEGER NOT NULL,
    UNIQUE (proposal_id, ordinal),
    CONSTRAINT proposed_plan_blocks_positive_duration
        CHECK (ends_at > starts_at),
    CONSTRAINT proposed_plan_blocks_ordinal_not_negative
        CHECK (ordinal >= 0)
);

CREATE INDEX proposed_plan_blocks_proposal_idx
    ON proposed_plan_blocks (proposal_id, ordinal);

CREATE TABLE planning_issues (
    id                TEXT PRIMARY KEY,
    proposal_id       TEXT NOT NULL REFERENCES plan_proposals (id),
    ordinal           INTEGER NOT NULL,
    code              TEXT NOT NULL,
    task_id           TEXT NULL REFERENCES tasks (id),
    message           TEXT NOT NULL,
    required_minutes  INTEGER NULL,
    scheduled_minutes INTEGER NULL,
    UNIQUE (proposal_id, ordinal),
    CONSTRAINT planning_issues_ordinal_not_negative CHECK (ordinal >= 0),
    CONSTRAINT planning_issues_message_not_blank CHECK (length(trim(message)) > 0)
);

CREATE INDEX planning_issues_proposal_idx ON planning_issues (proposal_id, ordinal);

CREATE TABLE commitment_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Existing (Phase 3A) state becomes the revision-0 baseline; nothing is reconstructed from
-- history, and every later mutation increments from here.
INSERT INTO commitment_meta (key, value) VALUES ('revision', '0');

-- plan_blocks gains provenance. SQLite cannot add a CHECK constraint, so the table is rebuilt:
-- new table -> copy rows (all existing blocks are manual) -> drop -> rename -> recreate indexes.
CREATE TABLE plan_blocks_new (
    id           TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL REFERENCES tasks (id),
    starts_at    TEXT NOT NULL,
    ends_at      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    cancelled_at TEXT NULL,
    origin       TEXT NOT NULL,
    proposal_id  TEXT NULL REFERENCES plan_proposals (id),
    CONSTRAINT plan_blocks_positive_duration CHECK (ends_at > starts_at),
    CONSTRAINT plan_blocks_origin_is_known CHECK (origin IN ('manual', 'planner')),
    CONSTRAINT plan_blocks_provenance_is_consistent
        CHECK ((origin = 'manual' AND proposal_id IS NULL)
            OR (origin = 'planner' AND proposal_id IS NOT NULL))
);

INSERT INTO plan_blocks_new (
    id, task_id, starts_at, ends_at, created_at, updated_at, cancelled_at, origin, proposal_id
)
SELECT id, task_id, starts_at, ends_at, created_at, updated_at, cancelled_at, 'manual', NULL
  FROM plan_blocks;

DROP TABLE plan_blocks;

ALTER TABLE plan_blocks_new RENAME TO plan_blocks;

CREATE INDEX plan_blocks_task_idx ON plan_blocks (task_id);
CREATE INDEX plan_blocks_starts_at_idx ON plan_blocks (starts_at);
CREATE INDEX plan_blocks_ends_at_idx ON plan_blocks (ends_at);
CREATE INDEX plan_blocks_origin_idx ON plan_blocks (origin, proposal_id);

