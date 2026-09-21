"""The deterministic attention projector and the read/settle service (ADR-0042).

```text
bounded source reads ──► AttentionCandidate list ──► reconcile against the live inbox
                                                          │
                        same fingerprint ──► nothing (idempotent)
                        new / changed     ──► open generation N+1
                        source gone       ──► RESOLVED
```

Two properties are the whole reason this module exists in this shape.

**It is not an agent.** The projector reads existing durable state and writes one derived row per
pending thing. There is no `ModelPort` in its constructor, no HTTP client, no socket and no
executor: "should this need the user's attention?" is answered by rules a reviewer can read, which
is exactly what makes the answer reproducible and testable.

**It is idempotent.** Refresh is periodic reconciliation, not an event stream. Running it once or
once a minute produces the same inbox, because identity is `dedupe_key` and change is
`fingerprint`. Event-driven refresh, when it exists, is only an optimisation (ADR-0042 §23-24).

Notifications are the one source that needs a *dedup* decision rather than a mapping. A
`plan_ready` reminder and the pending proposal behind it are one situation, not two, and a deadline
reminder and the task deadline behind it are likewise one. Those notification kinds are absorbed by
the structured projection that already describes them; only notifications with no structured
representation become items of their own.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from assistant.application.mail_send_status import MailDeliveryState, MailSendStatusService
from assistant.domain.attention import (
    ATTENTION_LIMIT,
    LIVE_ATTENTION_STATUSES,
    AttentionItem,
    AttentionItemId,
    AttentionKind,
    AttentionSeverity,
    AttentionSourceType,
    AttentionStatus,
    execution_unknown_dedupe_key,
    external_review_dedupe_key,
    fact_waiting_dedupe_key,
    mail_reply_dedupe_key,
    notification_dedupe_key,
    plan_block_dedupe_key,
    plan_waiting_dedupe_key,
    recurring_waiting_dedupe_key,
    source_fingerprint,
    task_dedupe_key,
    watcher_dedupe_key,
)
from assistant.domain.conversation import ConversationOperationStatus
from assistant.domain.conversation_plan import ConversationOperationType
from assistant.domain.conversation_review import (
    ConversationExternalReview,
    ConversationExternalReviewStatus,
)
from assistant.domain.errors import (
    AmbiguousAttentionReference,
    AttentionItemNotFound,
    DomainError,
)
from assistant.domain.fact import FactCandidateStatus
from assistant.domain.notification import Notification, NotificationKind
from assistant.domain.observation_analysis import ObservationCategory
from assistant.domain.plan_block import PlanBlock
from assistant.domain.planning import PlanProposalStatus
from assistant.domain.task import TaskPriority, TaskStatus
from assistant.ports.attention_repository import AttentionRepository
from assistant.ports.clock import Clock
from assistant.ports.commitment_repository import CommitmentRepository
from assistant.ports.conversation_repository import ConversationRepository
from assistant.ports.conversation_review_repository import ConversationReviewRepository
from assistant.ports.learning_repository import LearningRepository
from assistant.ports.mail_intelligence_repository import MailIntelligenceRepository
from assistant.ports.mail_repository import MailRepository
from assistant.ports.observation_analysis_repository import ObservationAnalysisRepository
from assistant.ports.planning_repository import PlanningRepository
from assistant.ports.scheduler_repository import SchedulerRepository
from assistant.ports.web_watch_repository import WebWatchRepository

LOGGER = logging.getLogger("assistant.attention")

DUE_SOON_DAYS = 3
"""How far ahead "a deadline is approaching" reaches, matching the Today Brief."""

SOURCE_SCAN_LIMIT = 200
"""How many rows of one source one refresh may read. Attention is an inbox, not a crawl."""

LIVE_INBOX_LIMIT = 500
"""How many live rows the reconciliation reads. Bounded by the sources, and checked here anyway."""

WATCHER_SCAN_LIMIT = 50
"""How many recent web observations one refresh inspects for actionable change."""

WATCHER_SUMMARY_CHARS = 300
"""How much of an analysis summary an inbox line may quote."""


@dataclass(frozen=True, slots=True)
class AttentionCandidate:
    """One situation a source says should be in the inbox right now."""

    kind: AttentionKind
    source_type: AttentionSourceType
    source_id: str
    dedupe_key: str
    fingerprint: str
    severity: AttentionSeverity
    title: str
    summary: str | None = None


@dataclass(frozen=True, slots=True)
class AttentionRefresh:
    """What one reconciliation actually did. Counts only; never content."""

    opened: int = 0
    refreshed: int = 0
    reopened: int = 0
    resolved: int = 0
    errors: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        """Whether anything a viewer would notice changed."""
        return bool(self.opened or self.reopened or self.resolved)


@dataclass(frozen=True, slots=True)
class AttentionSummary:
    """The bounded, product-level view of the inbox."""

    total: int
    items: tuple[AttentionItem, ...] = ()
    overflow: int = 0
    by_severity: dict[str, int] = field(default_factory=dict)


class AttentionProjector:
    """Reconciles the derived inbox with the durable sources behind it."""

    def __init__(
        self,
        *,
        items: AttentionRepository,
        commitments: CommitmentRepository,
        planning: PlanningRepository,
        scheduler: SchedulerRepository,
        conversations: ConversationRepository,
        reviews: ConversationReviewRepository,
        clock: Clock,
        mail: MailRepository | None = None,
        mail_intelligence: MailIntelligenceRepository | None = None,
        learning: LearningRepository | None = None,
        sends: MailSendStatusService | None = None,
        observations: WebWatchRepository | None = None,
        analyses: ObservationAnalysisRepository | None = None,
        planning_timezone: str | None = None,
        due_soon_days: int = DUE_SOON_DAYS,
    ) -> None:
        self._items = items
        self._commitments = commitments
        self._planning = planning
        self._scheduler = scheduler
        self._conversations = conversations
        self._reviews = reviews
        self._mail = mail
        self._mail_intelligence = mail_intelligence
        self._learning = learning
        self._sends = sends
        self._observations = observations
        self._analyses = analyses
        self._clock = clock
        self._timezone = planning_timezone
        self._due_soon_days = due_soon_days

    # ------------------------------------------------------------------------ projection

    async def project(self) -> tuple[AttentionCandidate, ...]:
        """The candidates this host's current state implies, in a stable order.

        One failing source must not empty the inbox of every other source, so each section is
        guarded: a source that raises contributes nothing and is logged, and the rest still stands.
        """
        candidates: list[AttentionCandidate] = []
        for section in (
            self._task_candidates,
            self._plan_block_candidates,
            self._mail_candidates,
            self._plan_candidates,
            self._fact_candidates,
            self._recurring_candidates,
            self._review_candidates,
            self._execution_candidates,
        ):
            try:
                candidates.extend(await section())
            except DomainError as exc:
                LOGGER.warning("attention source %s failed: %s", section.__name__, exc)
        candidates.extend(await self._notification_candidates())
        try:
            candidates.extend(await self._observation_candidates())
        except DomainError as exc:
            LOGGER.warning("attention source observations failed: %s", exc)
        return _deduplicate(tuple(candidates))

    async def refresh(self) -> AttentionRefresh:
        """Reconcile the inbox with the sources. Idempotent, bounded and non-fatal."""
        now = self._clock.now()
        candidates = await self.project()
        try:
            live = await self._items.list_items(
                statuses=LIVE_ATTENTION_STATUSES, limit=LIVE_INBOX_LIMIT
            )
        except DomainError as exc:  # pragma: no cover - a store failure surfaces to the caller
            raise AttentionItemNotFound(str(exc)) from exc
        by_key = {item.dedupe_key: item for item in live}
        desired = {candidate.dedupe_key for candidate in candidates}
        opened = refreshed = reopened = 0
        for candidate in candidates:
            existing = by_key.get(candidate.dedupe_key)
            if existing is None:
                await self._items.add_item(_opened(candidate, now))
                opened += 1
                continue
            if existing.fingerprint == candidate.fingerprint:
                if _words_changed(existing, candidate):
                    await self._items.update_item(
                        existing.refreshed(
                            severity=candidate.severity,
                            title=candidate.title,
                            summary=candidate.summary,
                            at=now,
                        )
                    )
                    refreshed += 1
                continue
            # The material state moved on. The old generation is closed and a new one opened, so a
            # dismissed reminder cannot hide a genuinely different situation.
            await self._items.update_item(existing.resolve(now))
            await self._items.add_item(
                existing.next_generation(
                    kind=candidate.kind,
                    fingerprint=candidate.fingerprint,
                    severity=candidate.severity,
                    title=candidate.title,
                    summary=candidate.summary,
                    at=now,
                )
            )
            reopened += 1
        resolved = 0
        for item in live:
            if item.dedupe_key in desired:
                continue
            await self._items.update_item(item.resolve(now))
            resolved += 1
        summary = AttentionRefresh(
            opened=opened, refreshed=refreshed, reopened=reopened, resolved=resolved
        )
        if summary.changed:
            LOGGER.info(
                "attention refreshed opened=%d reopened=%d resolved=%d",
                opened,
                reopened,
                resolved,
            )
        return summary

    # --------------------------------------------------------------------------- sources

    async def _task_candidates(self) -> list[AttentionCandidate]:
        """Open tasks whose deadline has passed or is close, in product words."""
        now = self._clock.now()
        zone = self._zone()
        today = now.astimezone(zone).date()
        tasks = await self._commitments.list_tasks(statuses=(TaskStatus.OPEN,))
        if not tasks:
            return []
        deadlines = await self._commitments.list_deadlines([task.id for task in tasks])
        candidates: list[AttentionCandidate] = []
        for task in tasks[:SOURCE_SCAN_LIMIT]:
            deadline = deadlines.get(task.id)
            if deadline is None:
                continue
            due_at = deadline.due_at
            local_due = due_at.astimezone(zone)
            overdue = due_at < now
            if not overdue and local_due.date() > today + timedelta(days=self._due_soon_days):
                continue
            kind = AttentionKind.TASK_OVERDUE if overdue else AttentionKind.TASK_DUE_SOON
            if overdue:
                severity = AttentionSeverity.HIGH
                summary = f"截止时间是 {local_due.strftime('%m-%d %H:%M')}。"
            else:
                severity = (
                    AttentionSeverity.HIGH
                    if (local_due.date() == today or task.priority is TaskPriority.HIGH)
                    else AttentionSeverity.NORMAL
                )
                summary = f"截止时间是 {local_due.strftime('%m-%d %H:%M')}。"
            candidates.append(
                AttentionCandidate(
                    kind=kind,
                    source_type=AttentionSourceType.TASK,
                    source_id=str(task.id),
                    dedupe_key=task_dedupe_key(task.id),
                    fingerprint=source_fingerprint(
                        {
                            "kind": kind.value,
                            "title": task.title,
                            "status": task.status.value,
                            "priority": task.priority.value,
                            "due_at": due_at.isoformat(),
                        }
                    ),
                    severity=severity,
                    title=f"「{task.title}」已经超过截止时间"
                    if overdue
                    else f"「{task.title}」临近截止",
                    summary=summary,
                )
            )
        return candidates

    async def _plan_block_candidates(self) -> list[AttentionCandidate]:
        """Planned time that has passed while the task behind it is still open (ADR-0044 §14).

        This says "the slot went by and the task is not finished". It deliberately does *not* say
        the user did no work: a WorkSession may exist, and only the user knows the difference.
        """
        now = self._clock.now()
        zone = self._zone()
        open_tasks = {
            task.id: task.title
            for task in await self._commitments.list_tasks(statuses=(TaskStatus.OPEN,))
        }
        if not open_tasks:
            return []
        window_start = now - timedelta(days=14)
        blocks = await self._commitments.list_plan_blocks_in_range(
            query_start=window_start, query_end=now
        )
        candidates: list[AttentionCandidate] = []
        for block in _recently_passed(blocks, now)[:SOURCE_SCAN_LIMIT]:
            title = open_tasks.get(block.task_id)
            if title is None:
                continue
            local_end = block.ends_at.astimezone(zone)
            candidates.append(
                AttentionCandidate(
                    kind=AttentionKind.PLAN_BLOCK_PASSED,
                    source_type=AttentionSourceType.PLAN_BLOCK,
                    source_id=str(block.id),
                    dedupe_key=plan_block_dedupe_key(block.id),
                    fingerprint=source_fingerprint(
                        {
                            "kind": AttentionKind.PLAN_BLOCK_PASSED.value,
                            "starts_at": block.starts_at.isoformat(),
                            "ends_at": block.ends_at.isoformat(),
                            "task_id": str(block.task_id),
                            "cancelled_at": None
                            if block.cancelled_at is None
                            else block.cancelled_at.isoformat(),
                        }
                    ),
                    severity=AttentionSeverity.NORMAL,
                    title="计划时间已经过去，但任务仍未完成。",
                    summary=(
                        f"「{title}」原本安排在 {local_end.strftime('%m-%d %H:%M')} 结束。"
                    ),
                )
            )
        return candidates

    async def _mail_candidates(self) -> list[AttentionCandidate]:
        """Stored messages an analysis says ask for an answer. Metadata only, never a body."""
        if self._mail is None or self._mail_intelligence is None:
            return []
        candidates: list[AttentionCandidate] = []
        messages = await self._mail.list_messages(limit=SOURCE_SCAN_LIMIT)
        for message in messages:
            analysis = await self._mail_intelligence.get_analysis(message.id)
            if analysis is None or not analysis.requires_reply:
                continue
            candidates.append(
                AttentionCandidate(
                    kind=AttentionKind.MAIL_REQUIRES_REPLY,
                    source_type=AttentionSourceType.MAIL_MESSAGE,
                    source_id=str(message.id),
                    dedupe_key=mail_reply_dedupe_key(message.id),
                    fingerprint=source_fingerprint(
                        {
                            "kind": AttentionKind.MAIL_REQUIRES_REPLY.value,
                            "analysis_version": analysis.analyzer_version,
                            "requires_reply": analysis.requires_reply,
                            "subject": message.subject,
                        }
                    ),
                    severity=AttentionSeverity.NORMAL,
                    title=f"「{message.subject or '（无主题）'}」需要回复",
                    summary=f"来自 {message.from_address or '（未知发件人）'}。",
                )
            )
        return candidates

    async def _plan_candidates(self) -> list[AttentionCandidate]:
        """Plan proposals waiting for a human decision. Applying one is the only way they run."""
        summaries = await self._planning.list_proposal_summaries(limit=SOURCE_SCAN_LIMIT)
        candidates: list[AttentionCandidate] = []
        for summary in summaries:
            proposal = summary.proposal
            if proposal.status is not PlanProposalStatus.PENDING:
                continue
            blocks = summary.block_count
            candidates.append(
                AttentionCandidate(
                    kind=AttentionKind.PLAN_WAITING,
                    source_type=AttentionSourceType.PLAN_PROPOSAL,
                    source_id=str(proposal.id),
                    dedupe_key=plan_waiting_dedupe_key(proposal.id),
                    fingerprint=source_fingerprint(
                        {
                            "kind": AttentionKind.PLAN_WAITING.value,
                            "status": proposal.status.value,
                            "input_fingerprint": proposal.input_fingerprint,
                            "block_count": blocks,
                            "issue_count": summary.issue_count,
                        }
                    ),
                    severity=AttentionSeverity.NORMAL,
                    title="有一份周计划等待你确认",
                    summary=(
                        f"这份提案安排了 {blocks} 个时间块，应用后才会生效。"
                        if blocks
                        else "这份提案还没有可安排的时间块，应用前你可以先看看说明。"
                    ),
                )
            )
        return candidates

    async def _recurring_candidates(self) -> list[AttentionCandidate]:
        """Weekly rules waiting for confirmation, one item per waiting turn."""
        waiting = await self._conversations.list_operations_by_status(
            ConversationOperationStatus.WAITING_CONFIRMATION
        )
        candidates: list[AttentionCandidate] = []
        seen_turns: set[object] = set()
        for operation in waiting:
            if (
                operation.operation_type
                is not ConversationOperationType.CALENDAR_RECURRING_CREATE_WEEKLY
            ):
                continue
            if operation.turn_id in seen_turns:
                continue
            seen_turns.add(operation.turn_id)
            candidates.append(
                AttentionCandidate(
                    kind=AttentionKind.RECURRING_WAITING,
                    source_type=AttentionSourceType.CONVERSATION_OPERATION,
                    source_id=str(operation.turn_id),
                    dedupe_key=recurring_waiting_dedupe_key(operation.turn_id),
                    fingerprint=source_fingerprint(
                        {
                            "kind": AttentionKind.RECURRING_WAITING.value,
                            "turn_id": str(operation.turn_id),
                            "operation": operation.operation_type.value,
                            "status": operation.status.value,
                        }
                    ),
                    severity=AttentionSeverity.NORMAL,
                    title="有固定安排等待你确认",
                    summary="确认之后，这些每周安排才会进入你的日历。",
                )
            )
        return candidates

    async def _fact_candidates(self) -> list[AttentionCandidate]:
        """Candidates the user proposed with their own words and has not ruled on yet.

        The *key* travels; the value does not. A candidate is a proposal about the user's personal
        life, and an inbox line is not the place to repeat it (ADR-0042 §15).
        """
        if self._learning is None:
            return []
        candidates: list[AttentionCandidate] = []
        for candidate in await self._learning.list_candidates(
            statuses=(FactCandidateStatus.PENDING,), limit=SOURCE_SCAN_LIMIT
        ):
            candidates.append(
                AttentionCandidate(
                    kind=AttentionKind.FACT_WAITING,
                    source_type=AttentionSourceType.FACT_CANDIDATE,
                    source_id=str(candidate.id),
                    dedupe_key=fact_waiting_dedupe_key(candidate.id),
                    fingerprint=source_fingerprint(
                        {
                            "kind": AttentionKind.FACT_WAITING.value,
                            "fact_key": candidate.fact_key,
                            "status": candidate.status.value,
                        }
                    ),
                    severity=AttentionSeverity.INFO,
                    title=f"「{candidate.fact_key}」等待你确认",
                    summary="确认之后才会被记住。",
                )
            )
        return candidates

    async def _review_candidates(self) -> list[AttentionCandidate]:
        """Prepared external actions waiting for an explicit human confirmation."""
        waiting = await self._reviews.list_by_status(
            ConversationExternalReviewStatus.WAITING
        )
        candidates: list[AttentionCandidate] = []
        for review in waiting[:SOURCE_SCAN_LIMIT]:
            label = _review_label(review)
            if label is None:
                continue
            candidates.append(
                AttentionCandidate(
                    kind=AttentionKind.EXTERNAL_REVIEW_WAITING,
                    source_type=AttentionSourceType.CONVERSATION_REVIEW,
                    source_id=str(review.id),
                    dedupe_key=external_review_dedupe_key(review.id),
                    fingerprint=source_fingerprint(
                        {
                            "kind": AttentionKind.EXTERNAL_REVIEW_WAITING.value,
                            "action_type": review.action_type,
                            "action_fingerprint": review.action_fingerprint,
                        }
                    ),
                    severity=AttentionSeverity.HIGH,
                    title=label,
                    summary="只有你亲自确认之后才会执行。",
                )
            )
        return candidates

    async def _execution_candidates(self) -> list[AttentionCandidate]:
        """External outcomes nobody can prove yet. Never described as a failure."""
        sends = self._sends
        if sends is None:
            return []
        candidates: list[AttentionCandidate] = []
        for status in await sends.list_statuses(limit=SOURCE_SCAN_LIMIT):
            if status.state is not MailDeliveryState.SENDING_UNKNOWN:
                continue
            run = status.execution
            if run is None:  # pragma: no cover - the state is derived from a run or a stale one
                continue
            action_type = status.action.action_type.value
            candidates.append(
                AttentionCandidate(
                    kind=AttentionKind.EXTERNAL_EXECUTION_UNKNOWN,
                    source_type=AttentionSourceType.EXECUTION_RUN,
                    source_id=str(run.id),
                    dedupe_key=execution_unknown_dedupe_key(run.id),
                    fingerprint=source_fingerprint(
                        {
                            "kind": AttentionKind.EXTERNAL_EXECUTION_UNKNOWN.value,
                            "run_id": str(run.id),
                            "action_type": action_type,
                        }
                    ),
                    severity=AttentionSeverity.HIGH,
                    title="有一个外部操作的结果仍不确定",
                    summary="我不会自动重试；请先确认它到底有没有发生。",
                )
            )
        return candidates

    async def _notification_candidates(self) -> list[AttentionCandidate]:
        """Unread notifications, split into ones worth their own line and ones already covered."""
        notifications = await self._scheduler.list_notifications(
            unread_only=True, limit=SOURCE_SCAN_LIMIT
        )
        items: list[AttentionCandidate] = []
        for notification in notifications:
            if notification.kind in _ABSORBED_NOTIFICATION_KINDS:
                # A structured projection already describes exactly this situation, so showing
                # both would be telling the user the same thing twice (ADR-0042 §6).
                continue
            items.append(_notification_candidate(notification))
        return items

    async def _observation_candidates(self) -> list[AttentionCandidate]:
        """Actionable web/manual observations, normalised — never a second analysis pipeline."""
        if self._observations is None or self._analyses is None:
            return []
        observations = await self._observations.list_observations(limit=WATCHER_SCAN_LIMIT)
        candidates: list[AttentionCandidate] = []
        for observation in observations:
            analysis = await self._analyses.get_analysis(observation.id)
            if analysis is None or analysis.category is not ObservationCategory.ACTIONABLE:
                continue
            candidates.append(
                AttentionCandidate(
                    kind=AttentionKind.WATCHER_OBSERVATION,
                    source_type=AttentionSourceType.WEB_OBSERVATION,
                    source_id=str(observation.id),
                    dedupe_key=watcher_dedupe_key(observation.id),
                    fingerprint=source_fingerprint(
                        {
                            "kind": AttentionKind.WATCHER_OBSERVATION.value,
                            "analysis_version": analysis.analyzer_version,
                            "input_fingerprint": analysis.input_fingerprint,
                        }
                    ),
                    severity=AttentionSeverity.INFO,
                    title="关注的页面有了值得看一眼的变化",
                    summary=_bounded_text(analysis.summary, WATCHER_SUMMARY_CHARS),
                )
            )
        return candidates

    def _zone(self) -> ZoneInfo:
        """The planning timezone, or UTC when this host has not chosen one."""
        if self._timezone is None:
            return ZoneInfo("UTC")
        try:
            return ZoneInfo(self._timezone)
        except (ZoneInfoNotFoundError, ValueError):  # pragma: no cover - config is validated
            return ZoneInfo("UTC")


class AttentionService:
    """List, acknowledge and dismiss the inbox. It cannot execute anything."""

    def __init__(self, *, items: AttentionRepository, clock: Clock) -> None:
        self._items = items
        self._clock = clock

    async def list_live(self, *, limit: int = ATTENTION_LIMIT) -> AttentionSummary:
        """The live inbox: most urgent first, then oldest, bounded."""
        items = await self._items.list_items(statuses=LIVE_ATTENTION_STATUSES, limit=limit + 1)
        bounded = tuple(items[:limit])
        counts: dict[str, int] = {"info": 0, "normal": 0, "high": 0}
        for item in bounded:
            counts[item.severity.value] = counts.get(item.severity.value, 0) + 1
        return AttentionSummary(
            total=len(bounded),
            items=bounded,
            overflow=max(0, len(items) - limit),
            by_severity=counts,
        )

    async def list_open(self, *, limit: int = ATTENTION_LIMIT) -> AttentionSummary:
        """Only the items nobody has acknowledged or dismissed yet."""
        return await self._summary((AttentionStatus.OPEN,), limit)

    async def list_all(self, *, limit: int = ATTENTION_LIMIT) -> AttentionSummary:
        """Every item, including the settled and resolved ones. History, bounded."""
        return await self._summary(None, limit)

    async def get(self, item_id: AttentionItemId) -> AttentionItem | None:
        """One item by identity."""
        return await self._items.get_item(item_id)

    async def acknowledge(self, item_id: AttentionItemId) -> AttentionItem:
        """Mark one item seen.

        Raises:
            AttentionItemNotFound: no such item.
        """
        item = await self._items.get_item(item_id)
        if item is None:
            raise AttentionItemNotFound(str(item_id))
        return await self._items.update_item(item.acknowledge(self._clock.now()))

    async def dismiss(self, item_id: AttentionItemId) -> AttentionItem:
        """Stop reminding about one item. The source is untouched.

        Raises:
            AttentionItemNotFound: no such item.
        """
        item = await self._items.get_item(item_id)
        if item is None:
            raise AttentionItemNotFound(str(item_id))
        return await self._items.update_item(item.dismiss(self._clock.now()))

    async def settle_by_reference(self, reference: str, *, dismiss: bool) -> AttentionItem:
        """Settle one item the user pointed at in words.

        A full UUID settles exactly that item. Anything else is matched against the *currently
        live, most recent* items by identity prefix and by title substring, and a reference that
        could mean more than one of them settles nothing (ADR-0042 §12).

        Raises:
            AttentionItemNotFound: the reference names nothing.
            AmbiguousAttentionReference: the reference names more than one thing.
        """
        text = reference.strip()
        if not text:
            raise AmbiguousAttentionReference("我需要知道你说的是哪一条。")
        direct = _as_uuid(text)
        if direct is not None:
            return await (self.dismiss(direct) if dismiss else self.acknowledge(direct))
        live = (await self.list_live(limit=ATTENTION_LIMIT)).items
        matches = [
            item
            for item in live
            if str(item.id).startswith(text.lower()) or text in item.title
        ]
        if not matches:
            raise AttentionItemNotFound(text)
        if len(matches) > 1:
            raise AmbiguousAttentionReference(
                "这几条都符合，请说得再具体一点：" + "；".join(item.title for item in matches[:5])
            )
        return await (self.dismiss(matches[0].id) if dismiss else self.acknowledge(matches[0].id))

    async def _summary(
        self, statuses: tuple[AttentionStatus, ...] | None, limit: int
    ) -> AttentionSummary:
        items = await self._items.list_items(statuses=statuses, limit=limit + 1)
        bounded = tuple(items[:limit])
        counts: dict[str, int] = {"info": 0, "normal": 0, "high": 0}
        for item in bounded:
            counts[item.severity.value] = counts.get(item.severity.value, 0) + 1
        return AttentionSummary(
            total=len(bounded),
            items=bounded,
            overflow=max(0, len(items) - limit),
            by_severity=counts,
        )


_ABSORBED_NOTIFICATION_KINDS = frozenset(
    {NotificationKind.PLAN_READY, NotificationKind.DEADLINE_REMINDER}
)
"""Notification kinds a structured projection already describes.

`plan_ready` says a proposal was created, and the pending proposal is that proposal. A deadline
reminder says a deadline is close, and the task's own deadline item says exactly that. Leaving them
un-absorbed is what produced two lines for one waiting thing in v1.2.
"""

_REVIEW_LABELS: dict[str, str] = {
    "mail.send": "有一封邮件等待你确认发送",
    "ehall.submit-certificate": "有一项学校系统的操作等待你确认",
}
"""How a waiting external review is named. A closed mapping: an action type with no product
sentence has no inbox line, because showing its internal name is not an option."""


def _review_label(review: ConversationExternalReview) -> str | None:
    return _REVIEW_LABELS.get(review.action_type)


def _notification_candidate(notification: Notification) -> AttentionCandidate:
    """One unread notification that no structured projection covers."""
    return AttentionCandidate(
        kind=AttentionKind.NOTIFICATION,
        source_type=AttentionSourceType.NOTIFICATION,
        source_id=str(notification.id),
        dedupe_key=notification_dedupe_key(notification.id),
        fingerprint=source_fingerprint(
            {
                "kind": AttentionKind.NOTIFICATION.value,
                "notification_kind": notification.kind.value,
                "title": notification.title,
            }
        ),
        severity=AttentionSeverity.INFO,
        title="有一项后台任务需要你检查",
        summary=_bounded_text(notification.body, WATCHER_SUMMARY_CHARS),
    )


def _opened(candidate: AttentionCandidate, at: datetime) -> AttentionItem:
    return AttentionItem(
        kind=candidate.kind,
        source_type=candidate.source_type,
        source_id=candidate.source_id,
        dedupe_key=candidate.dedupe_key,
        fingerprint=candidate.fingerprint,
        severity=candidate.severity,
        title=candidate.title,
        summary=candidate.summary,
        created_at=at,
        updated_at=at,
    )


def _words_changed(item: AttentionItem, candidate: AttentionCandidate) -> bool:
    return (
        item.severity is not candidate.severity
        or item.title != candidate.title
        or item.summary != candidate.summary
    )


def _recently_passed(blocks: list[PlanBlock], now: datetime) -> list[PlanBlock]:
    """Passed, uncancelled blocks, most recent first."""
    passed = [
        block for block in blocks if block.ends_at < now and block.cancelled_at is None
    ]
    passed.sort(key=lambda block: block.ends_at, reverse=True)
    return passed


def _deduplicate(candidates: tuple[AttentionCandidate, ...]) -> tuple[AttentionCandidate, ...]:
    """One candidate per dedupe key, first one wins, in a stable order."""
    seen: set[str] = set()
    unique: list[AttentionCandidate] = []
    for candidate in candidates:
        if candidate.dedupe_key in seen:
            continue
        seen.add(candidate.dedupe_key)
        unique.append(candidate)
    return tuple(unique)


def _bounded_text(value: str, limit: int) -> str | None:
    text = " ".join(value.split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _as_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


__all__ = [
    "DUE_SOON_DAYS",
    "LIVE_INBOX_LIMIT",
    "SOURCE_SCAN_LIMIT",
    "AttentionCandidate",
    "AttentionProjector",
    "AttentionRefresh",
    "AttentionService",
    "AttentionSummary",
]
