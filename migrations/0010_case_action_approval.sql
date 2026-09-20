-- 0010_case_action_approval.sql — the action/approval/execution boundary (ADR-0023).
--
-- Four tables, one chain: a `cases` row is the container, an `action_requests` row is one exact
-- prepared side effect, an `approvals` row is a human decision bound to that action's exact
-- fingerprint, and an `execution_runs` row is the record of one attempt.
--
-- The constraints are the point. They encode what the code must never be able to do:
--   * an action is PREPARED, EXECUTED or CANCELLED, with the timestamp that matches;
--   * every approval names the fingerprint it authorised, and at most one *active* approval may
--     exist for an action (`consumed_at IS NULL AND superseded_at IS NULL`);
--   * a challenge stores only a token hash — never a token — and always expires after it was
--     created;
--   * a run is finished exactly when its status says so, and only one run per action may be
--     RUNNING at a time.
CREATE TABLE cases (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    status       TEXT NOT NULL,

    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    completed_at TEXT NULL,
    cancelled_at TEXT NULL,

    CONSTRAINT cases_status_is_known
        CHECK (status IN ('open', 'completed', 'cancelled')),
    CONSTRAINT cases_title_not_blank CHECK (length(trim(title)) > 0),
    CONSTRAINT cases_open_has_no_terminal_stamp
        CHECK (status <> 'open' OR (completed_at IS NULL AND cancelled_at IS NULL)),
    CONSTRAINT cases_completed_has_completed_at
        CHECK (status <> 'completed' OR (completed_at IS NOT NULL AND cancelled_at IS NULL)),
    CONSTRAINT cases_cancelled_has_cancelled_at
        CHECK (status <> 'cancelled' OR (cancelled_at IS NOT NULL AND completed_at IS NULL))
);

CREATE INDEX cases_status_idx ON cases (status, updated_at);

CREATE TABLE action_requests (
    id           TEXT PRIMARY KEY,
    case_id      TEXT NOT NULL REFERENCES cases (id),

    action_type  TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,

    status       TEXT NOT NULL,

    created_at   TEXT NOT NULL,
    executed_at  TEXT NULL,
    cancelled_at TEXT NULL,

    CONSTRAINT action_requests_status_is_known
        CHECK (status IN ('prepared', 'executed', 'cancelled')),
    CONSTRAINT action_requests_type_not_blank CHECK (length(trim(action_type)) > 0),
    -- A namespaced identifier: a prefix and at least one more segment.
    CONSTRAINT action_requests_type_is_namespaced CHECK (instr(action_type, '.') > 1),
    CONSTRAINT action_requests_payload_is_json CHECK (json_valid(payload_json)),
    CONSTRAINT action_requests_fingerprint_is_sha256 CHECK (length(fingerprint) = 64),
    CONSTRAINT action_requests_prepared_has_no_stamp
        CHECK (status <> 'prepared' OR (executed_at IS NULL AND cancelled_at IS NULL)),
    CONSTRAINT action_requests_executed_has_executed_at
        CHECK (status <> 'executed' OR (executed_at IS NOT NULL AND cancelled_at IS NULL)),
    CONSTRAINT action_requests_cancelled_has_cancelled_at
        CHECK (status <> 'cancelled' OR (cancelled_at IS NOT NULL AND executed_at IS NULL))
);

CREATE INDEX action_requests_case_idx ON action_requests (case_id, created_at, id);
CREATE INDEX action_requests_status_idx ON action_requests (status, created_at);

CREATE TABLE approval_challenges (
    id                 TEXT PRIMARY KEY,
    action_id          TEXT NOT NULL REFERENCES action_requests (id),
    action_fingerprint TEXT NOT NULL,
    token_hash         TEXT NOT NULL,

    created_at         TEXT NOT NULL,
    expires_at         TEXT NOT NULL,
    consumed_at        TEXT NULL,

    CONSTRAINT approval_challenges_fingerprint_is_sha256
        CHECK (length(action_fingerprint) = 64),
    CONSTRAINT approval_challenges_token_hash_is_sha256 CHECK (length(token_hash) = 64),
    CONSTRAINT approval_challenges_expires_after_creation CHECK (expires_at > created_at)
);

-- A token hash identifies one challenge; the plaintext is never stored anywhere.
CREATE UNIQUE INDEX approval_challenges_token_idx ON approval_challenges (token_hash);
CREATE INDEX approval_challenges_action_idx ON approval_challenges (action_id, created_at);

CREATE TABLE approvals (
    id                 TEXT PRIMARY KEY,
    action_id          TEXT NOT NULL REFERENCES action_requests (id),
    action_fingerprint TEXT NOT NULL,

    approved_at        TEXT NOT NULL,
    expires_at         TEXT NOT NULL,
    consumed_at        TEXT NULL,
    superseded_at      TEXT NULL,

    CONSTRAINT approvals_fingerprint_is_sha256 CHECK (length(action_fingerprint) = 64),
    CONSTRAINT approvals_expire_after_approval CHECK (expires_at > approved_at)
);

-- At most one approval may be outstanding for an action. A superseded row is history, not
-- authority: it keeps the audit trail without blocking a later, explicit approval.
CREATE UNIQUE INDEX approvals_active_idx
    ON approvals (action_id)
    WHERE consumed_at IS NULL AND superseded_at IS NULL;
CREATE INDEX approvals_action_idx ON approvals (action_id, approved_at);

CREATE TABLE execution_runs (
    id            TEXT PRIMARY KEY,
    action_id     TEXT NOT NULL REFERENCES action_requests (id),
    approval_id   TEXT NOT NULL REFERENCES approvals (id),

    status        TEXT NOT NULL,

    started_at    TEXT NOT NULL,
    finished_at   TEXT NULL,
    error_summary TEXT NULL,

    CONSTRAINT execution_runs_status_is_known
        CHECK (status IN ('running', 'succeeded', 'failed', 'unknown')),
    CONSTRAINT execution_runs_finished_matches_status
        CHECK ((status = 'running') = (finished_at IS NULL))
);

CREATE UNIQUE INDEX execution_runs_running_idx
    ON execution_runs (action_id)
    WHERE status = 'running';
CREATE INDEX execution_runs_action_idx ON execution_runs (action_id, started_at, id);
