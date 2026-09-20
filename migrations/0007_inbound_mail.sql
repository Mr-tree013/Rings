-- 0007_inbound_mail.sql — durable inbound mail and the IMAP mailbox cursor (ADR-0020).
--
-- An IMAP UID is only meaningful together with the account, the mailbox and the UIDVALIDITY
-- the server reported when it issued that UID. So identity is split:
--
--   mail_messages          the logical message, with a stable internal UUID, the parsed
--                          headers, body text, fingerprints and the raw-object pointer
--   mail_message_locations where that message was seen: (account, mailbox, uidvalidity, uid)
--   mailbox_sync_state     the incremental cursor per mailbox (an optimisation and an
--                          incrementality record, never a proof of completeness)
--   mail_attachments       attachment metadata; the bytes stay inside the raw .eml object
--   mail_event_links       which MailMessage has been bridged to which InboundEvent
--
-- Message-ID is deliberately NOT unique: real mailboxes contain duplicates, missing headers
-- and forged ids. It is indexed evidence for reconciliation, not an identity.
CREATE TABLE mailbox_sync_state (
    account_id        TEXT NOT NULL,
    mailbox_name      TEXT NOT NULL,

    uidvalidity       INTEGER NOT NULL,
    last_seen_uid     INTEGER NOT NULL,

    mode              TEXT NOT NULL,
    last_sync_at      TEXT NULL,
    last_reconciled_at TEXT NULL,
    updated_at        TEXT NOT NULL,

    PRIMARY KEY (account_id, mailbox_name),
    CONSTRAINT mailbox_sync_state_mode_is_known
        CHECK (mode IN ('normal', 'reconciling')),
    CONSTRAINT mailbox_sync_state_uidvalidity_positive CHECK (uidvalidity > 0),
    CONSTRAINT mailbox_sync_state_cursor_not_negative CHECK (last_seen_uid >= 0)
);

CREATE TABLE mail_messages (
    id                 TEXT PRIMARY KEY,
    account_id         TEXT NOT NULL,

    message_id_header  TEXT NULL,
    in_reply_to_header TEXT NULL,
    references_json    TEXT NOT NULL,

    subject            TEXT NULL,
    from_address       TEXT NULL,
    to_addresses_json  TEXT NOT NULL,
    cc_addresses_json  TEXT NOT NULL,

    date_header        TEXT NULL,
    sent_at            TEXT NULL,

    body_text          TEXT NULL,
    body_status        TEXT NOT NULL,

    raw_sha256         TEXT NULL,
    content_fingerprint TEXT NOT NULL,
    raw_storage_key    TEXT NULL,

    size_bytes         INTEGER NOT NULL,
    parse_warnings     INTEGER NOT NULL,

    first_seen_at      TEXT NOT NULL,
    last_seen_at       TEXT NOT NULL,

    CONSTRAINT mail_messages_body_status_is_known
        CHECK (body_status IN ('available', 'oversize')),
    CONSTRAINT mail_messages_size_not_negative CHECK (size_bytes >= 0),
    CONSTRAINT mail_messages_warnings_not_negative CHECK (parse_warnings >= 0),
    CONSTRAINT mail_messages_fingerprint_is_sha256
        CHECK (length(content_fingerprint) = 64),
    CONSTRAINT mail_messages_raw_sha256_is_sha256
        CHECK (raw_sha256 IS NULL OR length(raw_sha256) = 64),
    CONSTRAINT mail_messages_oversize_has_no_body
        CHECK (body_status <> 'oversize' OR body_text IS NULL),
    CONSTRAINT mail_messages_raw_key_needs_hash
        CHECK (raw_storage_key IS NULL OR raw_sha256 IS NOT NULL)
);

CREATE INDEX mail_messages_account_idx ON mail_messages (account_id);
CREATE INDEX mail_messages_message_id_idx ON mail_messages (message_id_header);
CREATE INDEX mail_messages_fingerprint_idx ON mail_messages (content_fingerprint);
CREATE INDEX mail_messages_raw_sha_idx ON mail_messages (raw_sha256);
CREATE INDEX mail_messages_sent_at_idx ON mail_messages (sent_at);

CREATE TABLE mail_message_locations (
    message_id   TEXT NOT NULL REFERENCES mail_messages (id),
    account_id   TEXT NOT NULL,
    mailbox_name TEXT NOT NULL,

    uidvalidity  INTEGER NOT NULL,
    uid          INTEGER NOT NULL,

    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,

    UNIQUE (account_id, mailbox_name, uidvalidity, uid),
    CONSTRAINT mail_message_locations_uidvalidity_positive CHECK (uidvalidity > 0),
    CONSTRAINT mail_message_locations_uid_positive CHECK (uid > 0)
);

CREATE INDEX mail_message_locations_message_idx ON mail_message_locations (message_id);
CREATE INDEX mail_message_locations_mailbox_idx
    ON mail_message_locations (account_id, mailbox_name, uidvalidity);

CREATE TABLE mail_attachments (
    id                  TEXT PRIMARY KEY,
    message_id          TEXT NOT NULL REFERENCES mail_messages (id),
    ordinal             INTEGER NOT NULL,

    filename            TEXT NULL,
    content_type        TEXT NULL,
    content_disposition TEXT NULL,

    size_bytes          INTEGER NOT NULL,
    sha256              TEXT NOT NULL,

    UNIQUE (message_id, ordinal),
    CONSTRAINT mail_attachments_ordinal_not_negative CHECK (ordinal >= 0),
    CONSTRAINT mail_attachments_size_not_negative CHECK (size_bytes >= 0),
    CONSTRAINT mail_attachments_sha256_is_sha256 CHECK (length(sha256) = 64)
);

CREATE INDEX mail_attachments_message_idx ON mail_attachments (message_id, ordinal);

-- The bridge between durable mail and the unified external-input layer. One row per logical
-- message means "this MailMessage already has exactly one InboundEvent", which is what makes
-- both crash windows (message before event, event before link) repairable.
CREATE TABLE mail_event_links (
    mail_message_id  TEXT PRIMARY KEY REFERENCES mail_messages (id),
    inbound_event_id TEXT NOT NULL UNIQUE REFERENCES inbound_events (id),
    linked_at        TEXT NOT NULL
);

CREATE INDEX mail_event_links_event_idx ON mail_event_links (inbound_event_id);
