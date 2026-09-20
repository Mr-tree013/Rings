-- 0013_learning_facts.sql — durable corrections, candidates and confirmed facts (ADR-0027).
--
-- Three tables, and one rule that holds the whole phase together: a personal fact becomes
-- *trusted* only when a person promotes it by hand. Nothing in this schema can be written by a
-- model, a worker or a schedule; the only writer is the learning service behind an explicit CLI
-- command.
--
--   * `corrections` is what the user said, in their own words. It is the provenance of every
--     candidate and it is never deleted, so a fact can always be traced back to the sentence
--     that produced it;
--   * `fact_candidates` is a proposal: a key, a value, the correction it came from, and a
--     one-way status. A pending candidate is not trusted for anything;
--   * `confirmed_facts` is what a person promoted. `candidate_id` is UNIQUE, so one candidate
--     yields at most one fact, and the partial unique index allows at most one *current* row per
--     key. Superseded rows stay in the table: history is retired, never deleted, and expiry is a
--     derived read (`valid_until`) rather than a scheduled mutation.
--
-- The key column carries the same shape rule as the domain — lowercase, namespaced, at most 128
-- characters — as a backstop. The credential-like segment ban lives in the domain, where the
-- vocabulary belongs; SQLite cannot express "this segment is a secret" without becoming a list
-- that would drift.
CREATE TABLE corrections (
    id         TEXT PRIMARY KEY,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL,

    CONSTRAINT corrections_text_is_not_blank CHECK (length(trim(text)) > 0),
    CONSTRAINT corrections_text_max_length CHECK (length(text) <= 4000)
);

CREATE INDEX corrections_created_idx ON corrections (created_at, id);

CREATE TABLE fact_candidates (
    id                   TEXT PRIMARY KEY,
    fact_key             TEXT NOT NULL,
    value                TEXT NOT NULL,
    correction_id        TEXT NOT NULL REFERENCES corrections (id),

    status               TEXT NOT NULL,
    proposed_valid_until TEXT NULL,

    created_at           TEXT NOT NULL,
    resolved_at          TEXT NULL,

    CONSTRAINT fact_candidates_status_is_known
        CHECK (status IN ('pending', 'confirmed', 'rejected')),
    CONSTRAINT fact_candidates_key_shape
        CHECK (fact_key GLOB '[a-z][a-z0-9_.-]*' AND length(fact_key) <= 128),
    CONSTRAINT fact_candidates_value_is_not_blank CHECK (length(trim(value)) > 0),
    CONSTRAINT fact_candidates_value_max_length CHECK (length(value) <= 8000),
    CONSTRAINT fact_candidates_validity_follows_creation
        CHECK (proposed_valid_until IS NULL OR proposed_valid_until > created_at),
    -- A resolved candidate has a timestamp, a pending one does not: the two columns cannot drift.
    CONSTRAINT fact_candidates_resolution_matches_status CHECK (
        (status = 'pending' AND resolved_at IS NULL)
        OR (status <> 'pending' AND resolved_at IS NOT NULL)
    )
);

CREATE INDEX fact_candidates_status_idx ON fact_candidates (status, created_at);
CREATE INDEX fact_candidates_correction_idx ON fact_candidates (correction_id);
CREATE INDEX fact_candidates_key_idx ON fact_candidates (fact_key, created_at);

CREATE TABLE confirmed_facts (
    id           TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL UNIQUE REFERENCES fact_candidates (id),

    fact_key     TEXT NOT NULL,
    value        TEXT NOT NULL,

    valid_from   TEXT NOT NULL,
    valid_until  TEXT NULL,

    created_at   TEXT NOT NULL,
    superseded_at TEXT NULL,

    CONSTRAINT confirmed_facts_key_shape
        CHECK (fact_key GLOB '[a-z][a-z0-9_.-]*' AND length(fact_key) <= 128),
    CONSTRAINT confirmed_facts_value_is_not_blank CHECK (length(trim(value)) > 0),
    CONSTRAINT confirmed_facts_value_max_length CHECK (length(value) <= 8000),
    CONSTRAINT confirmed_facts_validity_orders CHECK (valid_until IS NULL OR valid_until > valid_from),
    CONSTRAINT confirmed_facts_superseded_after_creation
        CHECK (superseded_at IS NULL OR superseded_at >= created_at)
);

-- At most one *current* fact per key. Supersession is how a new value takes over; the partial
-- index is the database's half of "never two answers to the same question". Expiry is deliberately
-- not part of this index: an expired-but-current row still occupies the key, so confirming a new
-- value has to retire it atomically rather than tiptoe around a wall-clock expression.
CREATE UNIQUE INDEX confirmed_facts_current_idx
    ON confirmed_facts (fact_key)
    WHERE superseded_at IS NULL;

CREATE INDEX confirmed_facts_key_idx ON confirmed_facts (fact_key, created_at);
