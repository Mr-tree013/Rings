-- 0022_planning_preferences.sql — capacity rules and replanning (Phase 11D, ADR-0044).
--
-- Two additions, both about *intention* rather than fact:
--
--   planning_preferences         one row per runtime: the hours the user is willing to have
--                                planned, how much per day, and how long one sitting may be
--   plan_blocks.superseded_*     why a planner block stopped being current, recorded beside
--                                `cancelled_at` rather than replacing it
--
-- The planning *timezone* is deliberately absent. It already has exactly one authority in
-- `[planning].timezone`, and a second copy in a database column is how a machine ends up with two
-- ideas about which day "today" is.
--
-- A host with no preferences row behaves exactly as v1.2 did: `day_start_local = 0` and
-- `day_end_local = 1440` describe the whole civil day, which is what "the availability rules decide"
-- already meant. Nothing has to be migrated or backfilled for an existing runtime to keep working.

CREATE TABLE planning_preferences (
    id                       TEXT PRIMARY KEY,
    day_start_local          INTEGER NOT NULL,
    day_end_local            INTEGER NOT NULL,
    max_daily_minutes        INTEGER NOT NULL,
    preferred_block_minutes  INTEGER NOT NULL,
    max_block_minutes        INTEGER NOT NULL,
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,

    -- One row, addressed by a constant id, so "the current preferences" cannot be ambiguous.
    CONSTRAINT planning_preferences_is_a_singleton CHECK (id = 'current'),
    -- Minutes from local midnight. An overnight window is not supported, exactly as the weekly
    -- availability rules already refuse one.
    CONSTRAINT planning_preferences_day_is_ordered
        CHECK (0 <= day_start_local AND day_start_local < day_end_local
               AND day_end_local <= 1440),
    -- A day has 24 hours; a preference beyond that is a typo, not a policy.
    CONSTRAINT planning_preferences_daily_capacity_is_bounded
        CHECK (max_daily_minutes BETWEEN 1 AND 1440),
    -- A sitting is bounded by a day, and 30 minutes is the floor the planner already assumes.
    CONSTRAINT planning_preferences_block_is_bounded
        CHECK (preferred_block_minutes BETWEEN 1 AND max_block_minutes
               AND max_block_minutes <= 1440),
    CONSTRAINT planning_preferences_times_are_ordered CHECK (updated_at >= created_at)
);

-- Supersession is recorded *beside* cancellation, never instead of it: every existing query that
-- filters `cancelled_at IS NULL` keeps working, and the two facts stay distinguishable —
-- "the user cancelled this" versus "a later plan replaced it".
ALTER TABLE plan_blocks ADD COLUMN superseded_at TEXT NULL;
ALTER TABLE plan_blocks ADD COLUMN superseded_by_proposal_id TEXT NULL
    REFERENCES plan_proposals (id);

-- A block was superseded either by nobody, or by a proposal, at a moment.
-- (SQLite cannot add a multi-column CHECK with ALTER, so this is enforced by the store and by
-- `pw integrity check`, which both refuse the inconsistent shape.)
CREATE INDEX plan_blocks_supersession_idx
    ON plan_blocks (superseded_by_proposal_id, superseded_at)
    WHERE superseded_at IS NOT NULL;

-- Replanning is a deliberate mode, recorded on the proposal so a reviewer can tell "a fresh weekly
-- plan" from "replace what is left of this week".
ALTER TABLE plan_proposals ADD COLUMN mode TEXT NOT NULL DEFAULT 'normal';

CREATE INDEX plan_proposals_mode_idx ON plan_proposals (mode, status);
