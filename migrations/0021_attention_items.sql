-- 0021_attention_items.sql — the unified attention inbox (Phase 11B, ADR-0042).
--
-- Attention is *derived user-facing state*, not a second copy of the subsystems. One row says
-- "this durable source state currently needs a human", names the source it came from, and records
-- what the human did about it. The source rows stay exactly where they already are: nothing here
-- duplicates a task, a plan proposal, a mail message or an execution run.
--
-- Three properties carry the design:
--
--   live identity       UNIQUE(dedupe_key) among rows that are not resolved, so a projector that
--                       runs every minute writes one row per pending thing, not one per refresh
--   generation          a materially changed source resolves the old row and opens a new one, so
--                       "already dismissed" never silently hides a genuinely new situation
--   lifecycle           OPEN -> ACKNOWLEDGED / DISMISSED, and RESOLVED only from the projector or
--                       from the source going away; each state owns exactly its own timestamp
--
-- What is deliberately absent: mail bodies, credential values, provider responses, fact values,
-- file paths, model reasoning, and any approval or execution authority. An attention row can be
-- shown to a user and nothing else can be done with it.

CREATE TABLE attention_items (
    id                 TEXT PRIMARY KEY,

    kind               TEXT NOT NULL,
    source_type        TEXT NOT NULL,
    source_id          TEXT NOT NULL,

    dedupe_key         TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    generation         INTEGER NOT NULL DEFAULT 1,

    status             TEXT NOT NULL,
    severity           TEXT NOT NULL,

    title              TEXT NOT NULL,
    summary            TEXT NULL,

    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    acknowledged_at    TEXT NULL,
    dismissed_at       TEXT NULL,
    resolved_at        TEXT NULL,

    -- The closed kind vocabulary. A projector that wants to surface something new has to name it
    -- here first, which is what keeps "something the model thought was important" unrepresentable.
    CONSTRAINT attention_items_kind_is_known
        CHECK (kind IN ('task_overdue', 'task_due_soon', 'plan_block_passed',
                        'mail_requires_reply', 'plan_waiting', 'fact_waiting',
                        'recurring_waiting', 'external_review_waiting',
                        'external_execution_unknown', 'watcher_observation',
                        'notification')),
    CONSTRAINT attention_items_source_type_is_known
        CHECK (source_type IN ('task', 'deadline', 'plan_block', 'mail_message',
                               'plan_proposal', 'fact_candidate', 'recurring_rule',
                               'conversation_operation', 'conversation_review',
                               'execution_run', 'notification', 'web_observation')),
    CONSTRAINT attention_items_status_is_known
        CHECK (status IN ('open', 'acknowledged', 'dismissed', 'resolved')),
    CONSTRAINT attention_items_severity_is_known
        CHECK (severity IN ('info', 'normal', 'high')),

    CONSTRAINT attention_items_dedupe_key_is_bounded
        CHECK (length(trim(dedupe_key)) BETWEEN 1 AND 200),
    CONSTRAINT attention_items_source_id_is_bounded
        CHECK (length(trim(source_id)) BETWEEN 1 AND 200),
    CONSTRAINT attention_items_fingerprint_is_sha256
        CHECK (length(source_fingerprint) = 64
               AND source_fingerprint NOT GLOB '*[^0-9a-f]*'),
    CONSTRAINT attention_items_generation_is_positive CHECK (generation >= 1),
    CONSTRAINT attention_items_title_is_bounded
        CHECK (length(trim(title)) BETWEEN 1 AND 200),
    CONSTRAINT attention_items_summary_is_bounded
        CHECK (summary IS NULL OR length(summary) <= 500),

    -- Each lifecycle state owns exactly one timestamp, and the others stay NULL. That makes
    -- "dismissed, but somehow also acknowledged" a row the database refuses to hold.
    CONSTRAINT attention_items_lifecycle_consistency
        CHECK (
            (status = 'open'
                AND acknowledged_at IS NULL AND dismissed_at IS NULL AND resolved_at IS NULL)
            OR (status = 'acknowledged'
                AND acknowledged_at IS NOT NULL AND dismissed_at IS NULL AND resolved_at IS NULL)
            OR (status = 'dismissed'
                AND dismissed_at IS NOT NULL AND acknowledged_at IS NULL AND resolved_at IS NULL)
            OR (status = 'resolved'
                AND resolved_at IS NOT NULL
                AND acknowledged_at IS NULL AND dismissed_at IS NULL)
        ),
    CONSTRAINT attention_items_times_are_ordered
        CHECK (updated_at >= created_at)
);

-- The projector's idempotency guarantee: at most one live row per stable dedupe key, enforced by
-- the database rather than by the projector remembering to check.
CREATE UNIQUE INDEX attention_items_live_identity_idx
    ON attention_items (dedupe_key)
    WHERE status <> 'resolved';

-- The bounded inbox query: what is still live, most important and oldest first.
CREATE INDEX attention_items_status_idx
    ON attention_items (status, severity, created_at, id);

-- Source resolution: "which live items came from this source?".
CREATE INDEX attention_items_source_idx
    ON attention_items (source_type, source_id);
