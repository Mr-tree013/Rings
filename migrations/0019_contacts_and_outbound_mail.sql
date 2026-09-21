-- 0019_contacts_and_outbound_mail.sql — contacts and new outbound mail (ADR-0037).
--
-- Three additions, one story:
--
--   * `contacts` is local structured identity data used to turn a name a person wrote into an
--     address: bounded name and address, a normalized `email_key`, ACTIVE/RETIRED, a canonical
--     SHA-256 fingerprint and a partial unique index that makes "one live exact contact" a
--     database fact. A contact is not a `ConfirmedFact`, grants no authority and carries no
--     credential, code or prose;
--   * `new_mail_drafts` is the writable half of a *new* letter. Reply drafts keep their own table
--     and their source-message invariant: a reply always answers something, and forcing that
--     column nullable would have rewritten the meaning of every historical reply row;
--   * `mail_send_links` is rebuilt into a closed two-target variant. A prepared `mail.send` now
--     snapshots either a reply draft or a new-mail draft — exactly one of them — while keeping the
--     two constraints that make "one draft version, one action" and "one Message-ID, one action"
--     database facts. Every historical row keeps its meaning and its id.
--
-- Nothing here stores a credential, a password, a token, a mail body from the network or any
-- conversation text. Nothing here can send: sending still needs an `ActionRequest`, a human
-- `Approval` and an `ExecutionRun`, in that order (ADR-0023, ADR-0024, ADR-0037).

CREATE TABLE contacts (
    id                  TEXT PRIMARY KEY,

    display_name        TEXT NOT NULL,
    email_address       TEXT NOT NULL,
    -- Case-folded form of `email_address`. It is what "the same address" means, and it is stored
    -- rather than computed so a lookup never depends on SQLite's ASCII-only `lower()`.
    email_key           TEXT NOT NULL,

    status              TEXT NOT NULL,
    contact_fingerprint TEXT NOT NULL,

    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    retired_at          TEXT NULL,

    CONSTRAINT contacts_display_name_is_bounded
        CHECK (length(trim(display_name)) > 0 AND length(display_name) <= 120),
    CONSTRAINT contacts_address_is_bounded
        CHECK (length(trim(email_address)) > 0 AND length(email_address) <= 320),
    CONSTRAINT contacts_key_is_bounded
        CHECK (length(trim(email_key)) > 0 AND length(email_key) <= 320),
    -- The key is the address's own case-folded form, so a stored pair that disagrees with itself
    -- is refused here as well as by `pw integrity check`.
    CONSTRAINT contacts_key_matches_address CHECK (email_key = lower(trim(email_address))),
    CONSTRAINT contacts_status_is_known CHECK (status IN ('active', 'retired')),
    CONSTRAINT contacts_fingerprint_is_sha256
        CHECK (length(contact_fingerprint) = 64
               AND contact_fingerprint NOT GLOB '*[^0-9a-f]*'),
    CONSTRAINT contacts_retirement_consistency
        CHECK ((status = 'retired') = (retired_at IS NOT NULL))
);

-- One live exact contact. Two people may share a name (different address = different fingerprint),
-- and one address may carry more than one label; a *retired* contact can be recreated later.
CREATE UNIQUE INDEX contacts_active_identity_idx
    ON contacts (contact_fingerprint) WHERE status = 'active';

CREATE INDEX contacts_active_address_idx ON contacts (status, email_key);
CREATE INDEX contacts_status_idx ON contacts (status, display_name, id);

CREATE TABLE new_mail_drafts (
    id             TEXT PRIMARY KEY,
    account_id     TEXT NOT NULL,
    to_address     TEXT NOT NULL,

    subject        TEXT NOT NULL,
    body_text      TEXT NOT NULL,

    origin         TEXT NOT NULL,
    version        INTEGER NOT NULL,

    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,

    CONSTRAINT new_mail_drafts_account_not_blank CHECK (length(trim(account_id)) > 0),
    CONSTRAINT new_mail_drafts_recipient_not_blank
        CHECK (length(trim(to_address)) > 0 AND length(to_address) <= 320),
    CONSTRAINT new_mail_drafts_subject_not_blank
        CHECK (length(trim(subject)) > 0 AND length(subject) <= 2000),
    CONSTRAINT new_mail_drafts_body_not_blank CHECK (length(trim(body_text)) > 0),
    CONSTRAINT new_mail_drafts_origin_is_known
        CHECK (origin IN ('model_generated', 'user_edited')),
    CONSTRAINT new_mail_drafts_version_positive CHECK (version >= 1)
);

CREATE INDEX new_mail_drafts_updated_idx ON new_mail_drafts (updated_at, id);

-- Closed variant: exactly one of the two draft references is set, and the version belongs to it.
CREATE TABLE mail_send_links_rebuilt (
    action_id      TEXT PRIMARY KEY REFERENCES action_requests (id),
    draft_id       TEXT NULL REFERENCES mail_drafts (id),
    new_draft_id   TEXT NULL REFERENCES new_mail_drafts (id),
    draft_version  INTEGER NOT NULL,
    rfc_message_id TEXT NOT NULL,
    created_at     TEXT NOT NULL,

    CONSTRAINT mail_send_links_version_positive CHECK (draft_version >= 1),
    CONSTRAINT mail_send_links_message_id_not_blank
        CHECK (length(trim(rfc_message_id)) > 0),
    CONSTRAINT mail_send_links_one_draft_kind
        CHECK ((draft_id IS NULL) <> (new_draft_id IS NULL)),
    -- One draft version produces at most one send action: never two approvals for one letter.
    CONSTRAINT mail_send_links_one_per_draft_version UNIQUE (draft_id, draft_version),
    CONSTRAINT mail_send_links_one_per_new_draft_version UNIQUE (new_draft_id, draft_version)
);

INSERT INTO mail_send_links_rebuilt
    (action_id, draft_id, new_draft_id, draft_version, rfc_message_id, created_at)
    SELECT action_id, draft_id, NULL, draft_version, rfc_message_id, created_at
    FROM mail_send_links;

DROP TABLE mail_send_links;
ALTER TABLE mail_send_links_rebuilt RENAME TO mail_send_links;

CREATE UNIQUE INDEX mail_send_links_message_id_idx ON mail_send_links (rfc_message_id);
