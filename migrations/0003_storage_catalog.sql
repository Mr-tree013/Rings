-- 0003_storage_catalog.sql — stable storage roots and the metadata catalog (Phase 2A).
--
-- Identity lives in root_id + a POSIX relative path, never in a mount point:
-- `last_known_path` is runtime metadata that may change between scans, and it never
-- appears in a logical URI (ADR-0011).
--
-- The catalog stores filesystem metadata only. There is deliberately no content, summary,
-- hash, embedding or FTS table here: text extraction and search are a later phase.
CREATE TABLE storage_roots (
    root_id         TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    label           TEXT NOT NULL,
    last_known_path TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    last_scanned_at TEXT NULL,
    CONSTRAINT storage_roots_kind_is_known
        CHECK (kind IN ('local', 'vault')),
    CONSTRAINT storage_roots_root_id_shape
        CHECK (root_id GLOB '[a-z]*'
               AND root_id NOT GLOB '*[^a-z0-9-]*'
               AND length(root_id) <= 63),
    CONSTRAINT storage_roots_label_not_blank
        CHECK (length(trim(label)) > 0),
    CONSTRAINT storage_roots_path_not_blank
        CHECK (length(trim(last_known_path)) > 0)
);

CREATE TABLE catalog_entries (
    id                  TEXT PRIMARY KEY,
    root_id             TEXT NOT NULL REFERENCES storage_roots (root_id),
    relative_path       TEXT NOT NULL,
    name                TEXT NOT NULL,
    suffix              TEXT NOT NULL,
    size_bytes          INTEGER NOT NULL,
    mtime_ns            INTEGER NOT NULL,
    media_type          TEXT NULL,
    presence            TEXT NOT NULL,
    first_seen_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL,
    metadata_updated_at TEXT NOT NULL,
    last_seen_scan_id   TEXT NOT NULL,
    CONSTRAINT catalog_entries_presence_is_known
        CHECK (presence IN ('present', 'missing')),
    CONSTRAINT catalog_entries_size_not_negative
        CHECK (size_bytes >= 0),
    CONSTRAINT catalog_entries_mtime_not_negative
        CHECK (mtime_ns >= 0),
    CONSTRAINT catalog_entries_name_not_blank
        CHECK (length(trim(name)) > 0),
    CONSTRAINT catalog_entries_relative_path_not_blank
        CHECK (length(trim(relative_path)) > 0),
    CONSTRAINT catalog_entries_relative_path_is_relative
        CHECK (relative_path NOT LIKE '/%' AND relative_path NOT LIKE '\\%')
);

-- One catalog record per path per root: this is the document identity of this phase.
CREATE UNIQUE INDEX catalog_entries_root_path_key
    ON catalog_entries (root_id, relative_path);

CREATE INDEX catalog_entries_root_idx
    ON catalog_entries (root_id);

CREATE INDEX catalog_entries_root_presence_idx
    ON catalog_entries (root_id, presence);

CREATE INDEX catalog_entries_name_idx
    ON catalog_entries (name);

