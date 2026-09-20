-- 0018_recurring_calendar_rules.sql — weekly recurring civil-time commitments (ADR-0036).
--
-- A weekly class is authoritative calendar state, not a scheduled job and not a pile of
-- materialized `calendar_events`: the rule is stored once, and concrete occurrences are derived
-- deterministically for whatever range somebody asks about. The planner reads those derived
-- occurrences as busy time, so it can never place a plan block on top of a class.

CREATE TABLE recurring_calendar_rules (
    id               TEXT PRIMARY KEY,
    title            TEXT NOT NULL,

    weekday          INTEGER NOT NULL,
    start_local_time TEXT NOT NULL,
    end_local_time   TEXT NOT NULL,
    timezone         TEXT NOT NULL,

    starts_on        TEXT NOT NULL,
    ends_on          TEXT NULL,

    status           TEXT NOT NULL,
    rule_fingerprint TEXT NOT NULL,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    retired_at TEXT NULL,

    CONSTRAINT recurring_rules_weekday_is_iso CHECK (weekday BETWEEN 1 AND 7),
    CONSTRAINT recurring_rules_local_times_are_clock
        CHECK (start_local_time GLOB '[0-2][0-9]:[0-5][0-9]'
               AND end_local_time GLOB '[0-2][0-9]:[0-5][0-9]'),
    -- v1.1 is weekly, same-day, non-overnight: a rule that wrapped past midnight would need a
    -- second weekday and a second meaning, which this phase does not define.
    CONSTRAINT recurring_rules_end_after_start CHECK (end_local_time > start_local_time),
    CONSTRAINT recurring_rules_title_is_bounded
        CHECK (length(trim(title)) > 0 AND length(title) <= 200),
    CONSTRAINT recurring_rules_status_is_known CHECK (status IN ('active', 'retired')),
    CONSTRAINT recurring_rules_dates_are_ordered
        CHECK (ends_on IS NULL OR ends_on >= starts_on),
    CONSTRAINT recurring_rules_fingerprint_is_sha256
        CHECK (length(rule_fingerprint) = 64
               AND rule_fingerprint NOT GLOB '*[^0-9a-f]*'),
    CONSTRAINT recurring_rules_retirement_consistency
        CHECK ((status = 'retired') = (retired_at IS NOT NULL))
);

-- One live rule per exact weekly commitment; a retired rule may be recreated later.
CREATE UNIQUE INDEX recurring_calendar_rules_active_idx
    ON recurring_calendar_rules (rule_fingerprint)
    WHERE status = 'active';

CREATE INDEX recurring_calendar_rules_weekday_idx
    ON recurring_calendar_rules (status, weekday);
