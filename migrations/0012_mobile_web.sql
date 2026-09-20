-- 0012_mobile_web.sql — the same-LAN mobile control plane (ADR-0026).
--
-- Two tables, and one rule they exist to enforce: **the database never holds a usable secret.**
-- A pairing token and a web session are random 256-bit values; what is stored is their SHA-256,
-- so a copy of this file is not a key to the assistant.
--
--   * `mobile_pairing_tokens` is one-time and short-lived. `consumed_at` is the single-use marker,
--     and the unique token hash means two pairings can never collide;
--   * `mobile_sessions` is revocable and short-lived, and carries the CSRF token's hash next to the
--     session token's hash. `revoked_at` is how a lost phone is cut off, and `last_seen_at` is how
--     a user can tell which sessions are actually in use.
CREATE TABLE mobile_pairing_tokens (
    id          TEXT PRIMARY KEY,
    token_hash  TEXT NOT NULL,

    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    consumed_at TEXT NULL,

    CONSTRAINT mobile_pairing_tokens_hash_is_sha256 CHECK (length(token_hash) = 64),
    CONSTRAINT mobile_pairing_tokens_expire_after_creation CHECK (expires_at > created_at)
);

CREATE UNIQUE INDEX mobile_pairing_tokens_hash_idx
    ON mobile_pairing_tokens (token_hash);

CREATE TABLE mobile_sessions (
    id                TEXT PRIMARY KEY,
    session_hash      TEXT NOT NULL,
    csrf_hash         TEXT NOT NULL,

    created_at        TEXT NOT NULL,
    expires_at        TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL,
    revoked_at        TEXT NULL,

    CONSTRAINT mobile_sessions_session_hash_is_sha256 CHECK (length(session_hash) = 64),
    CONSTRAINT mobile_sessions_csrf_hash_is_sha256 CHECK (length(csrf_hash) = 64),
    CONSTRAINT mobile_sessions_expire_after_creation CHECK (expires_at > created_at)
);

CREATE UNIQUE INDEX mobile_sessions_hash_idx ON mobile_sessions (session_hash);
CREATE INDEX mobile_sessions_expiry_idx ON mobile_sessions (expires_at, revoked_at);
