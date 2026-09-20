-- 0009_mail_reply_drafts.sql — durable reply drafts (ADR-0022).
--
-- A draft is local state a user asked for. It is not an outbox: there is no `sent_at`, no
-- approval, no SMTP status and no ActionRequest link, because this phase has no sending path at
-- all. `origin` records whether the body came from a model or from a user's edit, and `version`
-- is the optimistic-concurrency token an edit must present.
--
-- `mail_draft_sources` records the knowledge the model said it used, resolved locally from the
-- exact evidence supplied in that request: identity and location only (root, entry, chunk,
-- logical URI, source span). The excerpt text is deliberately not copied here — the index owns
-- it — and no filesystem path is ever stored.
--
-- `mail_messages.reply_to_addresses_json` is added to the existing table with an empty default,
-- so every Phase 5A/5B row keeps its meaning and no data is rewritten.
ALTER TABLE mail_messages
    ADD COLUMN reply_to_addresses_json TEXT NOT NULL DEFAULT '[]';

CREATE TABLE mail_drafts (
    id                           TEXT PRIMARY KEY,
    account_id                   TEXT NOT NULL,
    thread_id                    TEXT NULL REFERENCES mail_threads (id),
    reply_to_message_id          TEXT NOT NULL REFERENCES mail_messages (id),

    to_addresses_json            TEXT NOT NULL,
    subject                      TEXT NOT NULL,
    body_text                    TEXT NOT NULL,
    needs_user_input_json        TEXT NOT NULL,

    origin                       TEXT NOT NULL,
    version                      INTEGER NOT NULL,

    generation_input_fingerprint TEXT NOT NULL,
    prompt_version               INTEGER NOT NULL,

    created_at                   TEXT NOT NULL,
    updated_at                   TEXT NOT NULL,

    CONSTRAINT mail_drafts_origin_is_known
        CHECK (origin IN ('model_generated', 'user_edited')),
    CONSTRAINT mail_drafts_version_positive CHECK (version >= 1),
    CONSTRAINT mail_drafts_prompt_version_positive CHECK (prompt_version >= 1),
    CONSTRAINT mail_drafts_subject_not_blank CHECK (length(trim(subject)) > 0),
    CONSTRAINT mail_drafts_body_not_blank CHECK (length(trim(body_text)) > 0),
    CONSTRAINT mail_drafts_recipients_is_json
        CHECK (json_valid(to_addresses_json) AND json_type(to_addresses_json) = 'array'),
    CONSTRAINT mail_drafts_input_is_json
        CHECK (json_valid(needs_user_input_json)
            AND json_type(needs_user_input_json) = 'array'),
    CONSTRAINT mail_drafts_fingerprint_is_sha256
        CHECK (length(generation_input_fingerprint) = 64)
);

CREATE INDEX mail_drafts_message_idx
    ON mail_drafts (reply_to_message_id, created_at);

CREATE INDEX mail_drafts_updated_idx ON mail_drafts (updated_at, id);

CREATE TABLE mail_draft_sources (
    draft_id         TEXT NOT NULL REFERENCES mail_drafts (id),
    ordinal          INTEGER NOT NULL,

    root_id          TEXT NOT NULL,
    entry_id         TEXT NOT NULL,
    chunk_id         TEXT NOT NULL,
    logical_uri      TEXT NOT NULL,
    source_span_json TEXT NOT NULL,

    CONSTRAINT mail_draft_sources_ordinal_not_negative CHECK (ordinal >= 0),
    CONSTRAINT mail_draft_sources_unique_ordinal UNIQUE (draft_id, ordinal)
);

CREATE INDEX mail_draft_sources_draft_idx
    ON mail_draft_sources (draft_id, ordinal);
