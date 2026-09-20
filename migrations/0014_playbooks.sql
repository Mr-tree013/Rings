-- 0014_playbooks.sql — reviewed, tested, non-executing playbooks (ADR-0028).
--
-- Three tables, and one thing none of them can do: execute anything. A playbook records that a
-- specific approved payload succeeded once and that the current local parser still understands
-- it. It carries no approval, grants no capability and cannot become an `ActionRequest`.
--
--   * `playbook_candidates` names one successful action a person chose to remember. The `UNIQUE`
--     on `source_action_id` is the deliberate part: one successful action can seed one reviewed
--     candidate, so a rejected candidate cannot be recreated by running the same execution again;
--   * `playbook_replay_tests` is the append-only audit of dry runs. `issue_codes_json` holds
--     bounded machine-readable codes, never a payload excerpt, an exception repr or a mail body,
--     and the two CHECK constraints keep "passed" and "failed" honest about that;
--   * `playbooks` is the promoted reference. It points at the candidate, the test that qualified
--     it, and the source action and run, so provenance survives without copying any payload.
--
-- Nothing here is writable by a model, a worker or a schedule: every write comes from an explicit
-- `pw playbook` command.
CREATE TABLE playbook_candidates (
    id                       TEXT PRIMARY KEY,
    name                     TEXT NOT NULL,
    note                     TEXT NOT NULL,

    source_action_id         TEXT NOT NULL UNIQUE REFERENCES action_requests (id),
    source_execution_run_id  TEXT NOT NULL REFERENCES execution_runs (id),
    source_action_type       TEXT NOT NULL,
    source_action_fingerprint TEXT NOT NULL,

    status                   TEXT NOT NULL,

    created_at               TEXT NOT NULL,
    resolved_at              TEXT NULL,

    CONSTRAINT playbook_candidates_status_is_known
        CHECK (status IN ('pending', 'promoted', 'rejected')),
    CONSTRAINT playbook_candidates_name_is_not_blank CHECK (length(trim(name)) > 0),
    CONSTRAINT playbook_candidates_name_max_length CHECK (length(name) <= 200),
    CONSTRAINT playbook_candidates_note_is_not_blank CHECK (length(trim(note)) > 0),
    CONSTRAINT playbook_candidates_note_max_length CHECK (length(note) <= 2000),
    CONSTRAINT playbook_candidates_fingerprint_is_sha256
        CHECK (length(source_action_fingerprint) = 64),
    CONSTRAINT playbook_candidates_action_type_shape
        CHECK (source_action_type GLOB '[a-z]*' AND length(source_action_type) <= 128),
    CONSTRAINT playbook_candidates_resolution_matches_status CHECK (
        (status = 'pending' AND resolved_at IS NULL)
        OR (status <> 'pending' AND resolved_at IS NOT NULL)
    )
);

CREATE INDEX playbook_candidates_status_idx
    ON playbook_candidates (status, created_at);
CREATE INDEX playbook_candidates_run_idx
    ON playbook_candidates (source_execution_run_id);

CREATE TABLE playbook_replay_tests (
    id                TEXT PRIMARY KEY,
    candidate_id      TEXT NOT NULL REFERENCES playbook_candidates (id),

    action_type       TEXT NOT NULL,
    contract_version  INTEGER NOT NULL,
    input_fingerprint TEXT NOT NULL,

    status            TEXT NOT NULL,
    issue_codes_json  TEXT NOT NULL,

    tested_at         TEXT NOT NULL,

    CONSTRAINT playbook_replay_tests_status_is_known
        CHECK (status IN ('passed', 'failed')),
    CONSTRAINT playbook_replay_tests_contract_version_is_positive
        CHECK (contract_version >= 1),
    CONSTRAINT playbook_replay_tests_fingerprint_is_sha256
        CHECK (length(input_fingerprint) = 64),
    CONSTRAINT playbook_replay_tests_action_type_shape
        CHECK (action_type GLOB '[a-z]*' AND length(action_type) <= 128),
    -- A pass carries no codes; a failure carries at least one. Both are lists of short codes.
    CONSTRAINT playbook_replay_tests_pass_has_no_issues
        CHECK (status <> 'passed' OR issue_codes_json = '[]'),
    CONSTRAINT playbook_replay_tests_failure_has_issues
        CHECK (status <> 'failed' OR issue_codes_json <> '[]')
);

CREATE INDEX playbook_replay_tests_candidate_idx
    ON playbook_replay_tests (candidate_id, tested_at DESC, id DESC);

CREATE TABLE playbooks (
    id                      TEXT PRIMARY KEY,
    candidate_id            TEXT NOT NULL UNIQUE REFERENCES playbook_candidates (id),

    name                    TEXT NOT NULL,
    note                    TEXT NOT NULL,
    action_type             TEXT NOT NULL,

    source_action_id        TEXT NOT NULL REFERENCES action_requests (id),
    source_execution_run_id TEXT NOT NULL REFERENCES execution_runs (id),
    source_action_fingerprint TEXT NOT NULL,

    replay_contract_version INTEGER NOT NULL,
    promoted_from_test_id   TEXT NOT NULL REFERENCES playbook_replay_tests (id),

    status                  TEXT NOT NULL,
    created_at              TEXT NOT NULL,
    retired_at              TEXT NULL,

    CONSTRAINT playbooks_status_is_known CHECK (status IN ('active', 'retired')),
    CONSTRAINT playbooks_name_is_not_blank CHECK (length(trim(name)) > 0),
    CONSTRAINT playbooks_name_max_length CHECK (length(name) <= 200),
    CONSTRAINT playbooks_note_is_not_blank CHECK (length(trim(note)) > 0),
    CONSTRAINT playbooks_note_max_length CHECK (length(note) <= 2000),
    CONSTRAINT playbooks_action_type_shape
        CHECK (action_type GLOB '[a-z]*' AND length(action_type) <= 128),
    CONSTRAINT playbooks_fingerprint_is_sha256
        CHECK (length(source_action_fingerprint) = 64),
    CONSTRAINT playbooks_contract_version_is_positive
        CHECK (replay_contract_version >= 1),
    CONSTRAINT playbooks_retirement_matches_status CHECK (
        (status = 'active' AND retired_at IS NULL)
        OR (status = 'retired' AND retired_at IS NOT NULL)
    )
);

CREATE INDEX playbooks_status_idx ON playbooks (status, created_at);
CREATE INDEX playbooks_action_idx ON playbooks (source_action_id, created_at);
