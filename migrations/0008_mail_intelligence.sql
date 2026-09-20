-- 0008_mail_intelligence.sql — deterministic mail threading and durable analysis (ADR-0021).
--
-- Threading is code, not model reasoning: `mail_thread_members` records the *decision* and the
-- header evidence behind it, including the cases where no safe decision exists (`unresolved`
-- when the parent was never stored, `ambiguous` when a referenced Message-ID matches more than
-- one stored message). Duplicate Message-IDs are legal, so a thread can never be chosen
-- arbitrarily.
--
-- `mail_analyses` is the durable result of the model step, keyed by message: one analyzed message
-- has exactly one current analysis, and the stored `input_fingerprint` plus `analyzer_version`
-- let an EventWorker retry reuse it instead of paying for the same analysis twice.
CREATE TABLE mail_threads (
    id         TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX mail_threads_account_idx ON mail_threads (account_id, updated_at);

CREATE TABLE mail_thread_members (
    message_id        TEXT PRIMARY KEY REFERENCES mail_messages (id),
    thread_id         TEXT NOT NULL REFERENCES mail_threads (id),
    parent_message_id TEXT NULL REFERENCES mail_messages (id),
    link_status       TEXT NOT NULL,
    link_evidence     TEXT NULL,
    linked_at         TEXT NOT NULL,

    CONSTRAINT mail_thread_members_status_is_known
        CHECK (link_status IN ('root', 'linked', 'unresolved', 'ambiguous')),
    -- A member is either linked to a parent, or explicitly not.
    CONSTRAINT mail_thread_members_parent_matches_status
        CHECK ((link_status = 'linked') = (parent_message_id IS NOT NULL))
);

CREATE INDEX mail_thread_members_thread_idx
    ON mail_thread_members (thread_id, linked_at, message_id);

CREATE TABLE mail_analyses (
    message_id             TEXT PRIMARY KEY REFERENCES mail_messages (id),
    analyzer_version       INTEGER NOT NULL,
    input_fingerprint      TEXT NOT NULL,

    category               TEXT NOT NULL,
    requires_reply         INTEGER NOT NULL,
    summary                TEXT NOT NULL,
    action_candidates_json TEXT NOT NULL,

    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,

    CONSTRAINT mail_analyses_version_positive CHECK (analyzer_version >= 1),
    CONSTRAINT mail_analyses_fingerprint_is_sha256 CHECK (length(input_fingerprint) = 64),
    CONSTRAINT mail_analyses_category_is_known
        CHECK (category IN ('ordinary_correspondence', 'receipt_result', 'actionable_notice',
                            'unknown')),
    CONSTRAINT mail_analyses_requires_reply_is_boolean CHECK (requires_reply IN (0, 1)),
    CONSTRAINT mail_analyses_summary_not_blank CHECK (length(trim(summary)) > 0),
    CONSTRAINT mail_analyses_candidates_is_json
        CHECK (json_valid(action_candidates_json)
            AND json_type(action_candidates_json) = 'array')
);

CREATE INDEX mail_analyses_category_idx ON mail_analyses (category, updated_at);
