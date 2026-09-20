-- 0011_approved_mail_send.sql — approved SMTP delivery (ADR-0024).
--
-- Three additions, one story:
--
--   * `mail_drafts.needs_user_input_acknowledged_at` records that a person has read the open
--     questions a draft carries. Preparing a send while questions are unanswered (and unread) is
--     refused, and any later body edit clears the acknowledgement again;
--   * `mail_send_links` ties one `mail.send` ActionRequest to the exact draft *version* it
--     snapshotted, and to the RFC Message-ID that was generated before approval. Two unique
--     constraints make "one action per draft version" and "one action per Message-ID" database
--     facts, so a re-prepare must first advance the draft (an edit or an acknowledgement);
--   * `mail_send_reconciliations` is the audit trail of Sent-folder lookups. FOUND can resolve an
--     unknown attempt to SUCCEEDED; NOT_FOUND proves nothing and never authorises a resend.
ALTER TABLE mail_drafts
    ADD COLUMN needs_user_input_acknowledged_at TEXT NULL;

CREATE TABLE mail_send_links (
    action_id      TEXT PRIMARY KEY REFERENCES action_requests (id),
    draft_id       TEXT NOT NULL REFERENCES mail_drafts (id),
    draft_version  INTEGER NOT NULL,
    rfc_message_id TEXT NOT NULL,
    created_at     TEXT NOT NULL,

    CONSTRAINT mail_send_links_version_positive CHECK (draft_version >= 1),
    CONSTRAINT mail_send_links_message_id_not_blank
        CHECK (length(trim(rfc_message_id)) > 0),
    -- One draft version produces at most one send action: never two approvals for one letter.
    CONSTRAINT mail_send_links_one_per_draft_version UNIQUE (draft_id, draft_version)
);

CREATE UNIQUE INDEX mail_send_links_message_id_idx ON mail_send_links (rfc_message_id);

CREATE TABLE mail_send_reconciliations (
    id               TEXT PRIMARY KEY,
    action_id        TEXT NOT NULL REFERENCES action_requests (id),
    execution_run_id TEXT NOT NULL REFERENCES execution_runs (id),

    result           TEXT NOT NULL,
    checked_at       TEXT NOT NULL,

    mailbox_name     TEXT NULL,
    uidvalidity      INTEGER NULL,
    uid              INTEGER NULL,

    CONSTRAINT mail_send_reconciliations_result_is_known
        CHECK (result IN ('found', 'not_found', 'ambiguous', 'unavailable')),
    -- A FOUND result names exactly where the message was seen; every other result names nowhere.
    CONSTRAINT mail_send_reconciliations_found_has_location
        CHECK ((result = 'found') = (mailbox_name IS NOT NULL AND uid IS NOT NULL)),
    CONSTRAINT mail_send_reconciliations_uid_positive CHECK (uid IS NULL OR uid >= 1),
    CONSTRAINT mail_send_reconciliations_uidvalidity_positive
        CHECK (uidvalidity IS NULL OR uidvalidity >= 1)
);

CREATE INDEX mail_send_reconciliations_action_idx
    ON mail_send_reconciliations (action_id, checked_at, id);
