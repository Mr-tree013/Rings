-- 0015_inbound_observations.sql — durable web observations and manual input (ADR-0029).
--
-- Six tables, and one rule that shapes all of them: **the database stores identity, not content.**
-- A watcher observation records which target changed and what the normalized content hashed to;
-- the text itself lives in the content-addressed snapshot store under the runtime data directory,
-- and only a relative `storage_key` is written here. An `InboundEvent` for a change carries the
-- observation id — never the page.
--
--   * `web_observations` is the version history. `is_baseline` marks the first successful fetch of
--     a URL, which deliberately produces no change event; everything after it points back at the
--     observation it replaced;
--   * `web_observation_event_links` and `manual_input_event_links` are the bridges to `EventInbox`.
--     Both are separate rows so the two crash windows (source committed without an event, event
--     without a link) are repairable by re-running an idempotent ingest;
--   * `manual_inputs` is text a person pasted in. It may be forwarded content from someone else, so
--     it is treated as untrusted quoted input downstream — and it is stored before it is bridged;
--   * `observation_analyses` is keyed by `inbound_event_id UNIQUE`: an EventWorker retry finds the
--     analysis it already paid for instead of calling the provider again.
CREATE TABLE web_observations (
    id                      TEXT PRIMARY KEY,
    target_id               TEXT NOT NULL,
    url                     TEXT NOT NULL,

    content_sha256          TEXT NOT NULL,
    storage_key             TEXT NOT NULL,

    previous_observation_id TEXT NULL REFERENCES web_observations (id),
    is_baseline             INTEGER NOT NULL,

    fetched_at              TEXT NOT NULL,

    CONSTRAINT web_observations_baseline_is_boolean CHECK (is_baseline IN (0, 1)),
    CONSTRAINT web_observations_target_id_shape
        CHECK (target_id GLOB '[a-z]*' AND length(target_id) <= 63),
    CONSTRAINT web_observations_hash_is_sha256 CHECK (length(content_sha256) = 64),
    -- A baseline replaces nothing; a change always names what it replaced.
    CONSTRAINT web_observations_baseline_replaces_nothing
        CHECK (is_baseline = 0 OR previous_observation_id IS NULL),
    CONSTRAINT web_observations_change_names_its_predecessor
        CHECK (is_baseline = 1 OR previous_observation_id IS NOT NULL)
);

CREATE INDEX web_observations_target_idx
    ON web_observations (target_id, fetched_at DESC, id DESC);

CREATE TABLE web_observation_event_links (
    observation_id   TEXT PRIMARY KEY REFERENCES web_observations (id),
    inbound_event_id TEXT NOT NULL REFERENCES inbound_events (id),
    linked_at        TEXT NOT NULL
);

CREATE TABLE web_watch_state (
    target_id             TEXT PRIMARY KEY,
    url                   TEXT NOT NULL,

    content_sha256        TEXT NULL,
    latest_observation_id TEXT NULL REFERENCES web_observations (id),

    etag                  TEXT NULL,
    last_modified         TEXT NULL,

    checks_since_full     INTEGER NOT NULL DEFAULT 0,

    last_checked_at       TEXT NULL,
    last_changed_at       TEXT NULL,
    updated_at            TEXT NOT NULL,

    CONSTRAINT web_watch_state_checks_are_non_negative CHECK (checks_since_full >= 0),
    CONSTRAINT web_watch_state_hash_is_sha256
        CHECK (content_sha256 IS NULL OR length(content_sha256) = 64)
);

CREATE TABLE manual_inputs (
    id             TEXT PRIMARY KEY,
    source         TEXT NOT NULL,
    text           TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    created_at     TEXT NOT NULL,

    CONSTRAINT manual_inputs_source_is_known
        CHECK (source IN ('manual', 'qq-forward', 'other')),
    CONSTRAINT manual_inputs_text_is_not_blank CHECK (length(trim(text)) > 0),
    CONSTRAINT manual_inputs_text_max_length CHECK (length(text) <= 20000),
    CONSTRAINT manual_inputs_hash_is_sha256 CHECK (length(content_sha256) = 64)
);

CREATE INDEX manual_inputs_created_idx ON manual_inputs (created_at DESC, id DESC);

CREATE TABLE manual_input_event_links (
    manual_input_id  TEXT PRIMARY KEY REFERENCES manual_inputs (id),
    inbound_event_id TEXT NOT NULL REFERENCES inbound_events (id),
    linked_at        TEXT NOT NULL
);

CREATE TABLE observation_analyses (
    id                    TEXT PRIMARY KEY,
    inbound_event_id      TEXT NOT NULL UNIQUE REFERENCES inbound_events (id),

    source_kind           TEXT NOT NULL,
    analyzer_version      INTEGER NOT NULL,
    input_fingerprint     TEXT NOT NULL,

    category              TEXT NOT NULL,
    summary               TEXT NOT NULL,
    action_candidates_json TEXT NOT NULL,

    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,

    CONSTRAINT observation_analyses_source_kind_is_known
        CHECK (source_kind IN ('web.page.changed', 'manual.input.received')),
    CONSTRAINT observation_analyses_analyzer_version_is_positive
        CHECK (analyzer_version >= 1),
    CONSTRAINT observation_analyses_fingerprint_is_sha256
        CHECK (length(input_fingerprint) = 64),
    CONSTRAINT observation_analyses_category_is_known
        CHECK (category IN ('informational', 'actionable', 'ignore', 'unknown')),
    CONSTRAINT observation_analyses_summary_is_bounded
        CHECK (length(trim(summary)) > 0 AND length(summary) <= 800)
);

CREATE INDEX observation_analyses_fingerprint_idx
    ON observation_analyses (input_fingerprint);
