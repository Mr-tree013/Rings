"""Planner application service: build reviewable weekly proposals (ADR-0015).

The service resolves a window, reads one consistent snapshot, computes remaining effort,
expands availability, runs the pure planner, fingerprints the input and stores a proposal.
It never writes plan blocks: that happens only through an explicit apply, which the repository
fences on the commitment revision.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from assistant.application.planning_availability import (
    generate_availability,
    generate_daily_windows,
)
from assistant.application.planning_effort import compute_remaining_effort
from assistant.application.planning_fingerprint import planning_fingerprint
from assistant.application.planning_preferences_service import PlanningPreferencesService
from assistant.application.recurring_calendar_service import RecurringCalendarService
from assistant.domain.config import PlanningConfig
from assistant.domain.errors import (
    PlanningNotConfigured,
    PlanningSnapshotChanged,
    PlanningStateUnstable,
    PlanProposalNotFound,
    PlanProposalNotPending,
    StalePlanProposal,
)
from assistant.domain.planning import (
    PlanningIssue,
    PlanningRequest,
    PlanningSnapshot,
    PlanningTask,
    PlanningWindow,
    PlanProposal,
    PlanProposalDetail,
    PlanProposalId,
    PlanProposalStatus,
    PlanProposalSummary,
)
from assistant.domain.planning_intervals import Interval, merge_intervals
from assistant.domain.planning_preferences import PlanningPreferences, ProposalMode
from assistant.ports.clock import Clock
from assistant.ports.planner import Planner
from assistant.ports.planning_repository import ApplyOutcome, ApplyResult, PlanningRepository

MAX_PLANNING_ATTEMPTS = 3
"""How many times the service re-plans when commitment state changes underneath it."""


class PlannerService:
    """Creates, shows and applies weekly plan proposals."""

    def __init__(
        self,
        planning: PlanningRepository,
        planner: Planner,
        config: PlanningConfig | None,
        clock: Clock,
        *,
        max_attempts: int = MAX_PLANNING_ATTEMPTS,
        recurring: RecurringCalendarService | None = None,
        preferences: PlanningPreferencesService | None = None,
    ) -> None:
        self._planning = planning
        self._planner = planner
        self._config = config
        self._clock = clock
        self._max_attempts = max(1, max_attempts)
        self._recurring = recurring
        self._preferences = preferences

    @property
    def config(self) -> PlanningConfig | None:
        """The planning preferences this service was built with."""
        return self._config

    def week_window(self, *, next_week: bool = False) -> PlanningWindow:
        """The current (or next) local calendar week, never starting in the past.

        Raises:
            PlanningNotConfigured: no `[planning]` section, so no timezone to plan in.
        """
        config = self.require_config()
        timezone = ZoneInfo(config.timezone)
        now = self._clock.now()
        local_now = now.astimezone(timezone)
        monday = datetime.combine(
            local_now.date() - timedelta(days=local_now.weekday()),
            time(0, 0),
            tzinfo=timezone,
        )
        if next_week:
            monday = monday + timedelta(days=7)
        end = monday + timedelta(days=7)
        start = monday if next_week else max(monday, now)
        return PlanningWindow(
            starts_at=start.astimezone(UTC),
            ends_at=end.astimezone(UTC),
            timezone=config.timezone,
        )

    def require_config(self) -> PlanningConfig:
        """Return the planning preferences or explain that they are missing."""
        if self._config is None:
            raise PlanningNotConfigured(
                "no [planning] section in the host config; add timezone and availability "
                "before planning"
            )
        return self._config

    async def _recurring_busy(
        self, window: PlanningWindow
    ) -> tuple[tuple[datetime, datetime], ...]:
        """Derived weekly commitments inside one window, as busy time (ADR-0036 §6).

        The model never sees this and never chooses it: the deterministic planner is simply told
        that a class is already taken, so it cannot place a plan block over one.
        """
        if self._recurring is None:
            return ()
        occurrences = await self._recurring.expand_range(
            window_start=window.starts_at, window_end=window.ends_at
        )
        return tuple(
            (occurrence.starts_at, occurrence.ends_at) for occurrence in occurrences
        )

    async def create_week_proposal(self, *, next_week: bool = False) -> PlanProposalDetail:
        """Create a proposal for the current (or next) local week."""
        return await self.create_proposal(self.week_window(next_week=next_week))

    def replan_window(self) -> PlanningWindow:
        """The rest of this local week: from now until the week ends (ADR-0044 §42).

        This is what "今天没做完的往后排" means in time. It is deliberately the *same* instant the
        weekly plan ends, so a replan cannot straddle two planning horizons and produce two
        proposals a user would have to reconcile by hand.
        """
        window = self.week_window()
        config = self.require_config()
        timezone = ZoneInfo(config.timezone)
        local_now = self._clock.now().astimezone(timezone)
        monday = datetime.combine(
            local_now.date() - timedelta(days=local_now.weekday()),
            time(0, 0),
            tzinfo=timezone,
        )
        end = monday + timedelta(days=7)
        return PlanningWindow(
            starts_at=window.starts_at,
            ends_at=end.astimezone(UTC),
            timezone=config.timezone,
        )

    async def create_replan(self) -> PlanProposalDetail:
        """Propose a replacement for what is left of this week.

        The result is an ordinary pending proposal in `REPLACE_FUTURE` mode: nothing is superseded
        until the user applies it (ADR-0044 §11-12).
        """
        return await self.create_proposal(self.replan_window(), mode=ProposalMode.REPLACE_FUTURE)

    async def create_proposal(
        self, window: PlanningWindow, *, mode: ProposalMode = ProposalMode.NORMAL
    ) -> PlanProposalDetail:
        """Plan `window` and store the result as a durable proposal.

        Raises:
            PlanningStateUnstable: commitment state changed on every attempt, so no proposal
                was stored.
        """
        config = self.require_config()
        preferences = await self._effective_preferences()
        for _ in range(self._max_attempts):
            snapshot = await self._planning.load_snapshot(window)
            tasks, pre_issues = planning_tasks(snapshot)
            request = PlanningRequest(
                window=window,
                tasks=tasks,
                availability=generate_availability(
                    window,
                    config,
                    day_window=(preferences.day_start_local, preferences.day_end_local),
                ),
                busy_intervals=busy_intervals(snapshot, await self._recurring_busy(window)),
                min_block_minutes=config.min_block_minutes,
                # The user's own ceiling wins over the configured one: the configuration is what
                # the host allows, the preference is what the person asked for (ADR-0044 §6).
                max_block_minutes=preferences.max_block_minutes,
                deadline_buffer_minutes=config.deadline_buffer_minutes,
                preferred_block_minutes=preferences.preferred_block_minutes,
                daily_capacity=generate_daily_windows(window, config, preferences),
            )
            result = self._planner.plan(request)
            proposal = PlanProposal(
                window=window,
                mode=mode,
                input_fingerprint=planning_fingerprint(
                    window=window, config=config, snapshot=snapshot
                ),
                input_revision=snapshot.revision,
                created_at=self._clock.now(),
            )
            try:
                await self._planning.create_proposal(
                    proposal,
                    blocks=result.blocks,
                    issues=pre_issues + result.issues,
                )
            except PlanningSnapshotChanged:
                continue
            detail = await self._planning.get_proposal_detail(proposal.id)
            if detail is None:  # pragma: no cover - defensive
                raise PlanningStateUnstable(
                    f"proposal {proposal.id} vanished right after it was stored"
                )
            return detail
        raise PlanningStateUnstable(
            f"planning input changed {self._max_attempts} times in a row; nothing was stored"
        )

    async def _effective_preferences(self) -> PlanningPreferences:
        """The capacity rules to plan under, or the configuration's own defaults."""
        if self._preferences is None:
            return PlanningPreferences(
                preferred_block_minutes=self.require_config().max_block_minutes,
                max_block_minutes=self.require_config().max_block_minutes,
            )
        return await self._preferences.effective()

    async def list_proposals(self, *, limit: int | None = 20) -> list[PlanProposal]:
        """List proposals, newest first."""
        if limit is not None and limit <= 0:
            raise ValueError("limit must be a positive integer or None")
        return await self._planning.list_proposals(limit=limit)

    async def list_proposal_summaries(
        self, *, limit: int | None = 20
    ) -> list[PlanProposalSummary]:
        """List proposals with their block and issue counts, newest first."""
        if limit is not None and limit <= 0:
            raise ValueError("limit must be a positive integer or None")
        return await self._planning.list_proposal_summaries(limit=limit)

    async def get_proposal_detail(self, reference: str) -> PlanProposalDetail:
        """Return a stored proposal exactly as it was created."""
        proposal_id = await self.resolve_proposal_id(reference)
        detail = await self._planning.get_proposal_detail(proposal_id)
        if detail is None:
            raise PlanProposalNotFound(proposal_id)
        return detail

    async def latest_pending_detail(self, window: PlanningWindow) -> PlanProposalDetail | None:
        """Return the newest pending proposal for this planning horizon, if any.

        The horizon is `(window_end, timezone)`: a week is the unit a user reviews, while
        `window_start` is clamped to "now" and therefore drifts by minutes between proposals.
        Rolling replanning uses this to avoid storing a proposal identical to one the user has
        not reviewed yet, and `create_proposal` supersedes on the same horizon.
        """
        for proposal in await self._planning.list_proposals(limit=None):
            if proposal.status is not PlanProposalStatus.PENDING:
                continue
            if (
                proposal.window.ends_at == window.ends_at
                and proposal.window.timezone == window.timezone
            ):
                return await self._planning.get_proposal_detail(proposal.id)
        return None

    async def current_revision(self) -> int:
        """The commitment revision the planning input is currently at.

        Every planner-relevant mutation bumps it inside its own transaction, so equality with
        a stored proposal's `input_revision` means "nothing that can change the plan changed".
        """
        return await self._planning.get_current_revision()

    async def current_fingerprint(self, window: PlanningWindow) -> str:
        """Fingerprint the planning input as it is right now."""
        config = self.require_config()
        snapshot = await self._planning.load_snapshot(window)
        return planning_fingerprint(window=window, config=config, snapshot=snapshot)

    async def resolve_proposal_id(self, reference: str) -> PlanProposalId:
        """Resolve a proposal id or unique prefix."""
        return await self._planning.resolve_proposal_id(reference)

    async def apply_proposal(self, reference: str) -> ApplyResult:
        """Apply a pending proposal, refusing anything whose input has changed.

        Raises:
            PlanProposalNotFound: no such proposal.
            PlanProposalNotPending: it was already applied, superseded or marked stale.
            StalePlanProposal: the planning input changed since it was created.
        """
        config = self.require_config()
        proposal_id = await self.resolve_proposal_id(reference)
        detail = await self._planning.get_proposal_detail(proposal_id)
        if detail is None:
            raise PlanProposalNotFound(proposal_id)
        proposal = detail.proposal
        if proposal.status is not PlanProposalStatus.PENDING:
            raise PlanProposalNotPending(proposal.id, proposal.status)
        snapshot = await self._planning.load_snapshot(proposal.window)
        current_fingerprint = planning_fingerprint(
            window=proposal.window, config=config, snapshot=snapshot
        )
        if current_fingerprint != proposal.input_fingerprint:
            await self._planning.mark_stale(proposal.id)
            raise StalePlanProposal(proposal.id)
        result = await self._planning.apply_proposal(
            proposal.id, applied_at=self._clock.now()
        )
        if result.outcome is ApplyOutcome.STALE:
            raise StalePlanProposal(proposal.id)
        if result.outcome is ApplyOutcome.NOT_PENDING:
            raise PlanProposalNotPending(result.proposal.id, result.proposal.status)
        return result


def planning_tasks(
    snapshot: PlanningSnapshot,
) -> tuple[tuple[PlanningTask, ...], tuple[PlanningIssue, ...]]:
    """Build the planner's task read model plus the issues that follow from the facts."""
    tasks: list[PlanningTask] = []
    issues: list[PlanningIssue] = []
    for task in snapshot.open_tasks:
        actual_seconds = snapshot.actual_work_seconds.get(task.id, 0)
        effort = compute_remaining_effort(
            task_id=task.id,
            estimated_minutes=task.estimated_minutes,
            actual_seconds=actual_seconds,
        )
        if effort.issue is not None:
            issues.append(effort.issue)
        deadline = snapshot.deadlines.get(task.id)
        tasks.append(
            PlanningTask(
                task_id=task.id,
                title=task.title,
                priority=task.priority,
                created_at=task.created_at,
                estimated_minutes=task.estimated_minutes,
                actual_seconds=actual_seconds,
                remaining_minutes=effort.remaining_minutes,
                deadline=None if deadline is None else deadline.due_at,
            )
        )
    return tuple(tasks), tuple(issues)


def busy_intervals(
    snapshot: PlanningSnapshot,
    extra: tuple[tuple[datetime, datetime], ...] = (),
) -> tuple[Interval, ...]:
    """Busy time for planning: calendar events plus *manual* plan blocks, merged.

    Deadlines never appear, work sessions never appear, and existing planner blocks are
    excluded because the proposal replaces them.
    """
    return tuple(
        merge_intervals(
            [
                (occurrence_start, occurrence_end)
                for occurrence_start, occurrence_end in extra
            ]
            + [(event.starts_at, event.ends_at) for event in snapshot.active_calendar_events]
            + [
                (block.starts_at, block.ends_at)
                for block in snapshot.active_manual_plan_blocks
            ]
        )
    )


__all__ = ["MAX_PLANNING_ATTEMPTS", "PlannerService", "busy_intervals", "planning_tasks"]
