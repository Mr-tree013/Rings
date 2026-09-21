"""Handlers: the only place where a conversation turn touches an application service (ADR-0033 §8).

Every handler calls the service that already owns the rule — `TaskService`, `CalendarService`,
`WorkService`, `PlannerService`, `GroundedAnswerService` and the scheduler repository. There is no
SQL here, no second task model and no second planner: the conversation layer is an orchestration
adapter over the core, so a task created from a sentence is created by exactly the code that
creates one from `pw task add`.

Handlers return structured `OperationResult` data. Turning that into Chinese prose is the
renderer's job (`conversation_render.py`), which keeps this module about calling services.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import cast

from assistant.application.attention import AttentionService, AttentionSummary
from assistant.application.calendar_service import CalendarService, CreateCalendarEvent
from assistant.application.case_service import CaseService
from assistant.application.contacts import ContactService
from assistant.application.conversation_capabilities.introspection import (
    CapabilitySnapshot,
)
from assistant.application.conversation_capabilities.registry import (
    ConfirmationPolicy,
    ConversationCapability,
    ConversationCapabilityRegistry,
    OperationHandler,
    OperationResult,
    PreflightCheck,
    PreflightContext,
)
from assistant.application.conversational_facts import ConversationalFactService
from assistant.application.grounded_answer import GroundedAnswerService
from assistant.application.mail_drafts import MailDraftService
from assistant.application.mail_send_actions import MailSendActionService
from assistant.application.mail_send_reconciliation import MailSendReconciliationService
from assistant.application.mail_send_status import MailDeliveryState, MailSendStatusService
from assistant.application.mail_sync import MailSyncService
from assistant.application.new_mail_drafts import NewMailDraftService
from assistant.application.planner_service import PlannerService
from assistant.application.recipient_resolution import (
    RecipientResolver,
    RecipientSource,
)
from assistant.application.recurring_calendar_service import RecurringCalendarService
from assistant.application.task_service import CreateTask, EditTask, TaskService
from assistant.application.today_brief import BriefEntry, TodayBrief, TodayBriefService
from assistant.application.work_service import WorkService
from assistant.domain.config import MailAccountConfig
from assistant.domain.contact import Contact
from assistant.domain.conversation_plan import (
    AttentionDismissArguments,
    AttentionListArguments,
    AttentionSettleArguments,
    BriefTodayArguments,
    CalendarCreateArguments,
    CalendarListArguments,
    CalendarRecurringCreateWeeklyArguments,
    CalendarRecurringEditArguments,
    CalendarRecurringListArguments,
    CalendarRecurringRetireArguments,
    ContactCreateArguments,
    ContactEditArguments,
    ContactListArguments,
    ContactRetireArguments,
    ConversationOperationArguments,
    ConversationOperationType,
    FactListArguments,
    FactProposeArguments,
    FactShowArguments,
    KnowledgeAskArguments,
    MailAccountsArguments,
    MailComposeNewArguments,
    MailListArguments,
    MailPrepareNewSendArguments,
    MailPrepareReplySendArguments,
    MailReconcileSendArguments,
    MailReplyDraftArguments,
    MailShowArguments,
    MailStatusArguments,
    MailSyncArguments,
    MailThreadArguments,
    NotificationListArguments,
    NotificationReadArguments,
    PlanApplyProposalArguments,
    PlanCurrentArguments,
    PlanProposeWeekArguments,
    StatusGetArguments,
    SystemCapabilitiesArguments,
    TaskClearDeadlineArguments,
    TaskCompleteArguments,
    TaskCreateArguments,
    TaskEditArguments,
    TaskListArguments,
    TaskSetDeadlineArguments,
    TaskShowArguments,
    WorkRecordArguments,
)
from assistant.domain.deadline import Deadline
from assistant.domain.errors import (
    ContactNotFound,
    ConversationCapabilityUnavailable,
    DomainError,
    ForbiddenFactKey,
    InvalidContact,
    InvalidFactKey,
    InvalidTimeInterval,
    MailRecipientUnresolved,
    RecurringRuleNotFound,
)
from assistant.domain.fact import (
    ConfirmedFact,
    FactCandidate,
    validate_fact_key,
    validate_fact_value,
)
from assistant.domain.grounded_answer import KnowledgeEvidence
from assistant.domain.knowledge import SourceSpanKind
from assistant.domain.mail import MailMessage
from assistant.domain.mail_analysis import MailAnalysis
from assistant.domain.mail_draft import MailDraft
from assistant.domain.new_mail_draft import NewMailDraft
from assistant.domain.planning import (
    PlanProposalDetail,
    PlanProposalStatus,
    PlanProposalSummary,
)
from assistant.domain.recurring_calendar import (
    RecurringCalendarOccurrence,
    RecurringCalendarRule,
    format_clock,
    weekday_label,
)
from assistant.domain.task import Task, TaskId, TaskStatus
from assistant.ports.clock import Clock
from assistant.ports.commitment_repository import CommitmentRepository
from assistant.ports.mail_draft_repository import MailDraftRepository
from assistant.ports.mail_intelligence_repository import MailIntelligenceRepository
from assistant.ports.mail_repository import MailRepository
from assistant.ports.scheduler_repository import SchedulerRepository

CALENDAR_LIST_LIMIT = 60
"""How many events and plan blocks one `calendar.list` answer may carry."""

NOTIFICATION_LIST_LIMIT = 20
"""How many notifications one `notification.list` answer may carry."""

MAIL_LIST_LIMIT = 25
"""How many messages one `mail.list` answer may carry."""

MAIL_BODY_EXCERPT_CHARS = 1200
"""How much of one message body `mail.show` may quote back to the user."""

MAIL_THREAD_LIMIT = 20
"""How many messages of one thread `mail.thread` may carry."""

RECURRING_LIST_LIMIT = 12
"""How many weekly commitments one listing may carry, matching the recent-entity bound."""

CONTACT_LIST_LIMIT = 12
"""How many contacts one listing may carry, matching the recent-entity bound."""

FACT_LIST_LIMIT = 12
"""How many long-term facts one listing may carry, matching the recent-entity bound."""

ATTENTION_LIST_LIMIT = 20
"""How many attention items one `attention.list` answer may carry."""


class ConversationHandlers:
    """Executes one allowed operation against the existing application services."""

    def __init__(
        self,
        *,
        tasks: TaskService,
        calendar: CalendarService,
        work: WorkService,
        planner: PlannerService,
        scheduler: SchedulerRepository,
        knowledge: GroundedAnswerService,
        commitments: CommitmentRepository,
        clock: Clock,
        recurring: RecurringCalendarService | None = None,
        contacts: ContactService | None = None,
        recipients: RecipientResolver | None = None,
        new_mail_drafts: NewMailDraftService | None = None,
        facts: ConversationalFactService | None = None,
        today: TodayBriefService | None = None,
        mail: MailRepository | None = None,
        mail_intelligence: MailIntelligenceRepository | None = None,
        mail_sync: MailSyncService | None = None,
        mail_drafts: MailDraftService | None = None,
        mail_sends: MailSendActionService | None = None,
        mail_send_status: MailSendStatusService | None = None,
        mail_reconciliation: MailSendReconciliationService | None = None,
        cases: CaseService | None = None,
        mail_drafts_repository: MailDraftRepository | None = None,
        capability_snapshot: CapabilitySnapshot | None = None,
        mail_accounts: tuple[MailAccountConfig, ...] = (),
        knowledge_limit: int = 8,
        attention: AttentionService | None = None,
    ) -> None:
        self._tasks = tasks
        self._calendar = calendar
        self._work = work
        self._planner = planner
        self._scheduler = scheduler
        self._knowledge = knowledge
        self._commitments = commitments
        self._clock = clock
        self._recurring = recurring
        self._contacts = contacts
        self._recipients = recipients
        self._new_mail_drafts = new_mail_drafts
        self._facts = facts
        self._today = today
        self._mail = mail
        self._mail_intelligence = mail_intelligence
        self._mail_sync = mail_sync
        self._mail_drafts = mail_drafts
        self._mail_sends = mail_sends
        self._mail_send_status = mail_send_status
        self._mail_reconciliation = mail_reconciliation
        self._cases = cases
        self._mail_drafts_repository = mail_drafts_repository
        self._capability_snapshot = capability_snapshot
        self._mail_accounts = mail_accounts
        self._knowledge_limit = knowledge_limit
        self._attention = attention

    # ------------------------------------------------------------------------- reads

    async def status_get(self, arguments: ConversationOperationArguments) -> OperationResult:
        _expect(StatusGetArguments, arguments)
        now = self._clock.now()
        open_tasks = await self._tasks.list_tasks()
        deadlines = (
            await self._commitments.list_deadlines([task.id for task in open_tasks])
            if open_tasks
            else {}
        )
        next_entry = _next_deadline(open_tasks, deadlines)
        unread = await self._scheduler.list_notifications(unread_only=True, limit=50)
        pending = await self._latest_pending_proposal()
        return OperationResult(
            kind="status",
            data={
                "open_tasks": len(open_tasks),
                "next_deadline": next_entry,
                "unread_notifications": len(unread),
                "pending_proposal": None if pending is None else _proposal_payload(pending)[0],
                "now": now.isoformat(),
            },
        )

    async def task_list(self, arguments: ConversationOperationArguments) -> OperationResult:
        include_terminal = _expect(TaskListArguments, arguments).include_terminal
        tasks = await self._tasks.list_tasks(include_terminal=include_terminal)
        deadlines = (
            await self._commitments.list_deadlines([task.id for task in tasks]) if tasks else {}
        )
        actual = (
            await self._work.actual_seconds_for_tasks([task.id for task in tasks])
            if tasks
            else {}
        )
        return OperationResult(
            kind="tasks",
            data={
                "tasks": [
                    _task_payload(task, deadlines.get(task.id), actual.get(task.id, 0))
                    for task in tasks
                ]
            },
        )

    async def task_show(self, arguments: ConversationOperationArguments) -> OperationResult:
        task_id = _expect(TaskShowArguments, arguments).task_id
        task = await self._tasks.require_task(task_id)
        deadline = await self._tasks.get_deadline(task_id)
        actual = await self._work.get_task_actual_seconds(task_id)
        return OperationResult(
            kind="task",
            ref=str(task.id),
            data={"task": _task_payload(task, deadline, actual)},
        )

    async def calendar_list(self, arguments: ConversationOperationArguments) -> OperationResult:
        days = _expect(CalendarListArguments, arguments).days
        now = self._clock.now()
        end = now + timedelta(days=days)
        events = await self._calendar.list_events(query_start=now, query_end=end)
        blocks = await self._commitments.list_plan_blocks_in_range(query_start=now, query_end=end)
        occurrences = (
            ()
            if self._recurring is None
            else await self._recurring.expand_range(window_start=now, window_end=end)
        )
        return OperationResult(
            kind="calendar",
            data={
                "window_days": days,
                "events": [
                    _interval_payload(event.title, event.starts_at, event.ends_at)
                    for event in events[:CALENDAR_LIST_LIMIT]
                ],
                # Derived, never stored: a weekly class shows up in the week it happens, with no
                # occurrence row and no synthetic identity anywhere near the model or the user.
                "recurring": [
                    _recurring_occurrence_payload(occurrence)
                    for occurrence in occurrences[:CALENDAR_LIST_LIMIT]
                ],
                "plan_blocks": [
                    _interval_payload(
                        str(block.task_id), block.starts_at, block.ends_at
                    )
                    for block in blocks[:CALENDAR_LIST_LIMIT]
                ],
            },
        )

    async def plan_current(self, arguments: ConversationOperationArguments) -> OperationResult:
        _expect(PlanCurrentArguments, arguments)
        summary = await self._latest_pending_proposal()
        if summary is None:
            return OperationResult(kind="proposal_absent", data={})
        detail = await self._planner.get_proposal_detail(str(summary.proposal.id))
        return OperationResult(kind="proposal", ref=str(summary.proposal.id), data=_detail(detail))

    async def notification_list(self, arguments: ConversationOperationArguments) -> OperationResult:
        unread_only = _expect(NotificationListArguments, arguments).unread_only
        notifications = await self._scheduler.list_notifications(
            unread_only=unread_only, limit=NOTIFICATION_LIST_LIMIT
        )
        return OperationResult(
            kind="notifications",
            data={
                "notifications": [
                    {
                        "id": str(notification.id),
                        "title": notification.title,
                        "kind": notification.kind.value,
                        "created_at": notification.created_at.isoformat(),
                    }
                    for notification in notifications
                ]
            },
        )

    async def knowledge_ask(self, arguments: ConversationOperationArguments) -> OperationResult:
        asked = _expect(KnowledgeAskArguments, arguments)
        result = await self._knowledge.answer(
            asked.question, root_id=asked.root_id, limit=self._knowledge_limit
        )
        by_id = result.evidence_by_id()
        sources = [
            {
                "id": str(evidence.id),
                "label": str(evidence.logical_uri),
                "span": _span(evidence),
            }
            for evidence in result.evidence
        ]
        return OperationResult(
            kind="answer",
            data={
                "status": result.answer.status.value,
                "segments": [
                    {"text": segment.text, "sources": [str(s) for s in segment.source_ids]}
                    for segment in result.answer.segments
                ],
                "reason": result.answer.reason,
                "sources": sources,
                "offline_roots": list(result.offline_roots),
                "cited": sorted(
                    {
                        str(source_id)
                        for segment in result.answer.segments
                        for source_id in segment.source_ids
                        if source_id in by_id
                    }
                ),
            },
        )

    # --------------------------------------------------------------------------- mail

    async def mail_status(self, arguments: ConversationOperationArguments) -> OperationResult:
        _expect(MailStatusArguments, arguments)
        mail, intelligence, sends = self._require_mail()
        stored = await mail.count_messages()
        statuses = await sends.list_statuses(limit=MAIL_LIST_LIMIT)
        unresolved = [
            status
            for status in statuses
            if status.state is MailDeliveryState.SENDING_UNKNOWN
        ]
        waiting = [
            status
            for status in statuses
            if status.state in (MailDeliveryState.DRAFT, MailDeliveryState.APPROVED)
        ]
        needing_reply = 0
        for message in await mail.list_messages(limit=MAIL_LIST_LIMIT):
            analysis = await intelligence.get_analysis(message.id)
            if analysis is not None and analysis.requires_reply:
                needing_reply += 1
        return OperationResult(
            kind="mail_status",
            data={
                "stored_messages": stored,
                "requires_reply": needing_reply,
                "waiting_sends": len(waiting),
                "unresolved_sends": len(unresolved),
            },
        )

    async def mail_sync(self, arguments: ConversationOperationArguments) -> OperationResult:
        synced = _expect(MailSyncArguments, arguments)
        service = self._mail_sync
        if service is None:
            raise ConversationCapabilityUnavailable(
                "no mail account is configured, so there is nothing to receive"
            )
        result = await service.sync_once(synced.account_id)
        return OperationResult(
            kind="mail_synced",
            data={
                "accounts": [
                    {
                        "account_id": account.account_id,
                        "status": account.status.value,
                        "new_messages": account.new_messages,
                        "matched_existing": account.matched_existing,
                    }
                    for account in result.accounts
                ]
            },
        )

    async def mail_list(self, arguments: ConversationOperationArguments) -> OperationResult:
        listed = _expect(MailListArguments, arguments)
        mail, intelligence, _ = self._require_mail()
        messages = await mail.list_messages(limit=min(listed.limit, MAIL_LIST_LIMIT))
        payload: list[dict[str, object]] = []
        for message in messages:
            analysis = await intelligence.get_analysis(message.id)
            if listed.requires_reply and not (
                analysis is not None and analysis.requires_reply
            ):
                continue
            payload.append(_mail_message_payload(message, analysis))
        return OperationResult(
            kind="mail_messages",
            data={"messages": payload, "requires_reply_filter": listed.requires_reply},
        )

    async def mail_show(self, arguments: ConversationOperationArguments) -> OperationResult:
        shown = _expect(MailShowArguments, arguments)
        mail, intelligence, _ = self._require_mail()
        message_id = await mail.resolve_message_id(shown.message_id)
        message = await mail.get_message(message_id)
        if message is None:  # pragma: no cover - resolution just found it
            from assistant.domain.errors import MailMessageNotFound

            raise MailMessageNotFound(message_id)
        analysis = await intelligence.get_analysis(message.id)
        body = message.body_text or ""
        truncated = len(body) > MAIL_BODY_EXCERPT_CHARS
        return OperationResult(
            kind="mail_message",
            ref=str(message.id),
            data={
                **_mail_message_payload(message, analysis),
                "body": body[:MAIL_BODY_EXCERPT_CHARS].strip(),
                "body_truncated": truncated,
                "body_available": message.body_status.value == "available",
            },
        )

    async def mail_thread(self, arguments: ConversationOperationArguments) -> OperationResult:
        asked = _expect(MailThreadArguments, arguments)
        _, intelligence, _ = self._require_mail()
        thread_id = await intelligence.resolve_thread_id(asked.thread_id)
        messages = await intelligence.list_thread_messages(thread_id)
        payload: list[dict[str, object]] = []
        for message in messages[:MAIL_THREAD_LIMIT]:
            analysis = await intelligence.get_analysis(message.id)
            payload.append(_mail_message_payload(message, analysis))
        return OperationResult(
            kind="mail_thread",
            ref=str(thread_id),
            data={"thread_id": str(thread_id), "messages": payload},
        )

    async def mail_reply_draft(self, arguments: ConversationOperationArguments) -> OperationResult:
        asked = _expect(MailReplyDraftArguments, arguments)
        mail, _, _ = self._require_mail()
        drafts = self._require_drafts()
        message_id = await mail.resolve_message_id(asked.message_id)
        result = await drafts.create_reply_draft(
            message_id, context_query=asked.context_query
        )
        draft = result.draft
        if asked.body_text is not None and asked.body_text.strip() != draft.body_text.strip():
            # The user said what to write ("说我周五之前交"): the durable draft carries exactly
            # that, through the existing edit path, as a new draft version.
            draft = await drafts.edit_draft(draft.id, body=asked.body_text)
        return OperationResult(
            kind="mail_draft",
            ref=str(draft.id),
            data={
                "draft_id": str(draft.id),
                "version": draft.version,
                "subject": draft.subject,
                "to_addresses": list(draft.to_addresses),
                "body": draft.body_text,
                "needs_user_input": list(draft.needs_user_input),
                "reply_to_message_id": str(draft.reply_to_message_id),
            },
        )

    async def mail_prepare_reply_send(
        self, arguments: ConversationOperationArguments
    ) -> OperationResult:
        asked = _expect(MailPrepareReplySendArguments, arguments)
        sends = self._mail_sends
        cases = self._cases
        if sends is None or cases is None:
            raise ConversationCapabilityUnavailable(
                "sending needs a configured mail account and an outbound credential"
            )
        draft = await self._resolve_draft_for_prepare(asked)
        case = await cases.create_case(f"Reply: {draft.subject}"[:120])
        preparation = await sends.prepare_send(draft.id, case.id)
        payload = preparation.payload
        return OperationResult(
            kind="mail_prepared",
            ref=str(preparation.action.id),
            data={
                "action_id": str(preparation.action.id),
                "case_id": str(case.id),
                "draft_id": str(draft.id),
                "draft_version": payload.draft_version,
                "account_id": payload.account_id,
                "from_address": payload.from_address,
                "to_addresses": list(payload.to_addresses),
                "subject": payload.subject,
                "body_text": payload.body_text,
                "in_reply_to_header": payload.in_reply_to_header,
                "references": list(payload.references),
                "rfc_message_id": payload.rfc_message_id,
            },
        )

    async def mail_reconcile_send(
        self, arguments: ConversationOperationArguments
    ) -> OperationResult:
        asked = _expect(MailReconcileSendArguments, arguments)
        sends = self._mail_send_status
        reconciliation = self._mail_reconciliation
        if sends is None or reconciliation is None:
            raise ConversationCapabilityUnavailable(
                "reconciliation needs a configured mail account"
            )
        reference = asked.action_id
        if reference is None:
            statuses = await sends.list_statuses(limit=1)
            if not statuses:
                return OperationResult(kind="mail_reconciliation_absent", data={})
            reference = str(statuses[0].action.id)
        outcome = await reconciliation.reconcile(reference)
        return OperationResult(
            kind="mail_reconciled",
            ref=str(outcome.reconciliation.action_id)
            if outcome.reconciliation is not None
            else reference,
            data={
                "result": outcome.result.value,
                "already_sent": outcome.already_sent,
                "resolved": outcome.resolved,
            },
        )

    async def mail_accounts(self, arguments: ConversationOperationArguments) -> OperationResult:
        """Which mailboxes are configured — a different question from how much mail is stored."""
        _expect(MailAccountsArguments, arguments)
        accounts: list[dict[str, object]] = []
        mail = self._mail
        for account in self._mail_accounts:
            stored = 0
            if mail is not None:
                stored = await mail.count_messages(account_id=account.id)
            accounts.append(
                {
                    "account_id": account.id,
                    # Safe configuration metadata only: never a password, token or key.
                    "username": account.username,
                    "from_address": account.from_address,
                    "host": account.host,
                    "enabled": account.enabled,
                    "receive_configured": True,
                    "send_configured": bool(account.smtp_host and account.from_address),
                    "stored_messages": stored,
                }
            )
        return OperationResult(kind="mail_accounts", data={"accounts": accounts})

    async def mail_compose_new(
        self, arguments: ConversationOperationArguments
    ) -> OperationResult:
        """Write, or revise, one new-mail draft. Nothing is prepared and nothing is sent here."""
        asked = _expect(MailComposeNewArguments, arguments)
        drafts = self._require_new_drafts()
        resolver = self._require_recipients()
        current = None if asked.draft_id is None else await self._require_new_draft(asked.draft_id)
        # A revision that names neither a sender nor a recipient keeps both: the user asked to
        # change the text, and re-deriving the rest would be the runtime editing on its own.
        sender = (
            None
            if current is not None and asked.sender_account is None
            else await resolver.resolve_sender(asked.sender_account)
        )
        if sender is not None:
            account_id = sender.account_id
        elif current is not None:
            account_id = current.account_id
        else:  # pragma: no cover - `resolve_sender` returns or raises for a new letter
            raise ConversationCapabilityUnavailable("我还不知道从哪个邮箱发送")
        kind = asked.recipient_kind
        if kind is None:
            if current is None:  # pragma: no cover - the arguments require a recipient
                raise ConversationCapabilityUnavailable("我没有看到收件人")
            to_address = current.to_address
        else:
            recipient = await resolver.resolve_recipient(
                kind=RecipientSource(kind.value),
                address=asked.recipient_address,
                name=asked.recipient_name,
                sender=sender,
            )
            to_address = recipient.address
        if current is None:
            draft, created = await drafts.compose(
                account_id=account_id,
                to_address=to_address,
                subject=asked.subject,
                body_text=asked.body,
            )
        else:
            draft = await drafts.revise(
                current,
                account_id=account_id,
                to_address=to_address,
                subject=asked.subject,
                body_text=asked.body,
            )
            created = False
        return OperationResult(
            kind="new_mail_draft",
            ref=str(draft.id),
            data=_new_mail_draft_payload(draft, created=created),
        )

    async def mail_prepare_new_send(
        self, arguments: ConversationOperationArguments
    ) -> OperationResult:
        """Freeze one new-mail draft version into an immutable `mail.send` action.

        This prepares; it never approves and never executes (ADR-0034 §10, ADR-0037 §17).
        """
        asked = _expect(MailPrepareNewSendArguments, arguments)
        sends = self._mail_sends
        cases = self._cases
        if sends is None or cases is None:
            raise ConversationCapabilityUnavailable(
                "sending needs a configured mail account and an outbound credential"
            )
        self._require_new_drafts()
        draft = await self._require_new_draft(str(asked.draft_id))
        case = await cases.create_case(f"New mail: {draft.subject}"[:120])
        preparation = await sends.prepare_new_send(draft.id, case.id)
        payload = preparation.payload
        return OperationResult(
            kind="mail_prepared",
            ref=str(preparation.action.id),
            data={
                "action_id": str(preparation.action.id),
                "case_id": str(case.id),
                "draft_id": str(draft.id),
                "draft_version": payload.draft_version,
                "account_id": payload.account_id,
                "from_address": payload.from_address,
                "to_addresses": list(payload.to_addresses),
                "subject": payload.subject,
                "body_text": payload.body_text,
                "in_reply_to_header": payload.in_reply_to_header,
                "references": list(payload.references),
                "rfc_message_id": payload.rfc_message_id,
            },
        )

    async def contact_list(self, arguments: ConversationOperationArguments) -> OperationResult:
        listed = _expect(ContactListArguments, arguments)
        contacts = await self._require_contacts().list_contacts(
            include_retired=listed.include_retired
        )
        return OperationResult(
            kind="contacts",
            data={
                "contacts": [
                    _contact_payload(contact) for contact in contacts[:CONTACT_LIST_LIMIT]
                ],
                "include_retired": listed.include_retired,
            },
        )

    async def contact_create(self, arguments: ConversationOperationArguments) -> OperationResult:
        created = _expect(ContactCreateArguments, arguments)
        contact, was_created = await self._require_contacts().create(
            display_name=created.display_name, email_address=created.email_address
        )
        return OperationResult(
            kind="contact_created",
            ref=str(contact.id),
            data={**_contact_payload(contact), "created": was_created},
        )

    async def contact_edit(self, arguments: ConversationOperationArguments) -> OperationResult:
        edited = _expect(ContactEditArguments, arguments)
        contact = await self._require_contacts().edit(
            edited.contact_id,
            display_name=edited.display_name,
            email_address=edited.email_address,
        )
        return OperationResult(
            kind="contact_updated",
            ref=str(contact.id),
            data=_contact_payload(contact),
        )

    async def contact_retire(self, arguments: ConversationOperationArguments) -> OperationResult:
        retired = _expect(ContactRetireArguments, arguments)
        contact = await self._require_contacts().retire(retired.contact_id)
        return OperationResult(
            kind="contact_retired",
            ref=str(contact.id),
            data=_contact_payload(contact),
        )

    async def fact_list(self, arguments: ConversationOperationArguments) -> OperationResult:
        """The long-term facts a person has confirmed — keys, values and nothing else."""
        _expect(FactListArguments, arguments)
        overviews = await self._require_facts().fact_overviews(limit=FACT_LIST_LIMIT)
        now = self._clock.now()
        return OperationResult(
            kind="facts",
            data={"facts": [_fact_payload(item.fact, now=now) for item in overviews]},
        )

    async def fact_show(self, arguments: ConversationOperationArguments) -> OperationResult:
        """One confirmed fact by key, or the honest answer that nothing is confirmed for it."""
        asked = _expect(FactShowArguments, arguments)
        facts = self._require_facts()
        fact = await facts.show(asked.key)
        if fact is not None:
            return OperationResult(
                kind="fact",
                ref=str(fact.id),
                data={"fact": _fact_payload(fact, now=self._clock.now())},
            )
        # A false negative would be worse than a plain answer, so the reply names what *is*
        # confirmed (bounded), in case the model guessed a different key for the same question.
        known = await facts.list_facts(limit=FACT_LIST_LIMIT)
        return OperationResult(
            kind="fact_absent",
            data={
                "key": asked.key,
                "known_keys": [item.fact_key for item in known],
            },
        )

    async def fact_propose(self, arguments: ConversationOperationArguments) -> OperationResult:
        """Store a reviewable fact proposal. It is never confirmed here (ADR-0038 §4-§5)."""
        proposed = _expect(FactProposeArguments, arguments)
        result = await self._require_facts().propose(
            key=proposed.key,
            value=proposed.value,
            correction_text=proposed.correction_text,
        )
        return OperationResult(
            kind="fact_proposed",
            ref=str(result.candidate.id),
            data={
                **_fact_payload(result.candidate),
                "correction_text": result.correction_text,
            },
        )

    async def brief_today(self, arguments: ConversationOperationArguments) -> OperationResult:
        """The user's own today, read deterministically from local state and written nowhere."""
        _expect(BriefTodayArguments, arguments)
        service = self._today
        if service is None:
            raise ConversationCapabilityUnavailable("这台主机还不能生成今日概览")
        brief = await service.build()
        return OperationResult(kind="today_brief", data=_brief_payload(brief))

    async def system_capabilities(
        self, arguments: ConversationOperationArguments
    ) -> OperationResult:
        """What this build can do here, from the registry and the loaded configuration."""
        _expect(SystemCapabilitiesArguments, arguments)
        snapshot = self._capability_snapshot
        if snapshot is None:  # pragma: no cover - the composition root always supplies one
            raise ConversationCapabilityUnavailable(
                "this host cannot describe its own capabilities"
            )
        return OperationResult(
            kind="capabilities",
            data={"areas": snapshot.to_payload(), "operations": list(snapshot.operation_types)},
        )

    # --------------------------------------------------------------------- attention

    async def attention_list(self, arguments: ConversationOperationArguments) -> OperationResult:
        """The unified inbox: what the user's own state says needs them, in product language."""
        arguments = _expect(AttentionListArguments, arguments)
        service = self._require_attention()
        # Reconciled first: "最近有什么需要我处理的" has to describe this moment, not whatever the
        # last daemon interval happened to write.
        await service.refresh()
        summary = (
            await service.list_all(limit=ATTENTION_LIST_LIMIT)
            if arguments.include_settled
            else await service.list_live(limit=ATTENTION_LIST_LIMIT)
        )
        return OperationResult(
            kind="attention", data=_attention_payload(summary, self._clock.now())
        )

    async def attention_acknowledge(
        self, arguments: ConversationOperationArguments
    ) -> OperationResult:
        """Mark one item seen. It settles the reminder, never the thing it points at."""
        arguments = _expect(AttentionSettleArguments, arguments)
        service = self._require_attention()
        await service.refresh()
        item = await service.settle_by_reference(arguments.reference, dismiss=False)
        return OperationResult(
            kind="attention_settled",
            data={"title": item.title, "status": item.status.value},
        )

    async def attention_dismiss(
        self, arguments: ConversationOperationArguments
    ) -> OperationResult:
        """Stop reminding about one item. The source object is not touched."""
        arguments = _expect(AttentionDismissArguments, arguments)
        service = self._require_attention()
        await service.refresh()
        item = await service.settle_by_reference(arguments.reference, dismiss=True)
        return OperationResult(
            kind="attention_settled",
            data={"title": item.title, "status": item.status.value},
        )

    def _require_attention(self) -> AttentionService:
        service = self._attention
        if service is None:
            raise ConversationCapabilityUnavailable(
                "这台主机还不能汇总需要处理的事项"
            )
        return service

    # ------------------------------------------------------------------------ mail support

    async def preflight_task(
        self, arguments: ConversationOperationArguments, context: PreflightContext
    ) -> str | None:
        """A task operation must name a task that exists, before anything else runs."""
        task_id = _task_reference(arguments)
        if task_id is None:  # pragma: no cover - only task operations reach this checker
            return None
        if await self._commitments.get_task(task_id) is None:
            return f"找不到这个任务（{str(task_id)[:8]}）"
        return None

    async def preflight_mail_message(
        self, arguments: ConversationOperationArguments, context: PreflightContext
    ) -> str | None:
        """A mail read or draft must name a message that is stored."""
        message_id = _mail_message_reference(arguments)
        if message_id is None or self._mail is None:
            return None
        try:
            resolved = await self._mail.resolve_message_id(str(message_id))
        except Exception:
            return "找不到这封邮件"
        return None if resolved is not None else "找不到这封邮件"

    async def preflight_mail_draft(
        self, arguments: ConversationOperationArguments, context: PreflightContext
    ) -> str | None:
        """Preparing a send needs a draft that exists for the message it names."""
        prepared = (
            arguments if isinstance(arguments, MailPrepareReplySendArguments) else None
        )
        draft_id = None if prepared is None else prepared.draft_id
        message_id = None if prepared is None else prepared.message_id
        drafts = self._mail_drafts
        if drafts is None:
            return "这个主机没有可用的草稿服务"
        if draft_id is not None:
            try:
                await drafts.get_draft(str(draft_id))
            except Exception:
                return "找不到这份草稿"
            return None
        if message_id is None:
            return "没有指定要发送的草稿"
        if ConversationOperationType.MAIL_REPLY_DRAFT in context.preceding:
            # This turn drafts the reply first, so the draft does not exist yet — and that is fine.
            return None
        for listing in await drafts.list_drafts(limit=MAIL_LIST_LIMIT):
            if str(listing.draft.reply_to_message_id) == str(message_id):
                return None
        return "这封邮件还没有草稿，我需要先起草一封回复"

    async def preflight_proposal(
        self, arguments: ConversationOperationArguments, context: PreflightContext
    ) -> str | None:
        """Applying a proposal needs one that is still pending."""
        proposal_id = (
            arguments.proposal_id
            if isinstance(arguments, PlanApplyProposalArguments)
            else None
        )
        try:
            if proposal_id is None:
                summary = await self._latest_pending_proposal()
                return None if summary is not None else "现在没有待审阅的周计划提案"
            await self._planner.get_proposal_detail(str(proposal_id))
        except Exception:
            return "找不到这份周计划提案"
        return None

    async def preflight_recurring(
        self, arguments: ConversationOperationArguments, context: PreflightContext
    ) -> str | None:
        """A weekly commitment needs a timezone and, when it names one, a real rule.

        Both checks are read-only and run before the first mutation of the turn, so a missing
        planning timezone or a rule that cannot be resolved leaves durable state untouched
        (ADR-0036 §6-§8, ADR-0035 §15-§16).
        """
        service = self._recurring
        if service is None:
            return "这台主机还没有可以保存固定安排的地方"
        if isinstance(arguments, CalendarRecurringCreateWeeklyArguments):
            if arguments.timezone is None and service.default_timezone is None:
                return (
                    f"你希望我按哪个时区理解{weekday_label(arguments.weekday)} "
                    f"{arguments.start_local_time}–{arguments.end_local_time} 的"
                    f"「{arguments.title}」？配置里现在没有 [planning].timezone。"
                )
            return None
        reference = _recurring_reference(arguments)
        if reference is None:  # pragma: no cover - only recurring operations reach this check
            return None
        try:
            await service.require_rule(reference)
        except RecurringRuleNotFound:
            return f"找不到这条固定安排（{reference[:8]}）"
        except InvalidTimeInterval as exc:
            return str(exc)
        return None

    def _require_recurring(self) -> RecurringCalendarService:
        if self._recurring is None:
            raise ConversationCapabilityUnavailable("这台主机还没有可用的固定安排存储")
        return self._recurring

    def _require_contacts(self) -> ContactService:
        if self._contacts is None:
            raise ConversationCapabilityUnavailable("这台主机还没有可用的联系人存储")
        return self._contacts

    def _require_recipients(self) -> RecipientResolver:
        if self._recipients is None:
            raise ConversationCapabilityUnavailable("这台主机还不能解析收件人")
        return self._recipients

    def _require_new_drafts(self) -> NewMailDraftService:
        if self._new_mail_drafts is None:
            raise ConversationCapabilityUnavailable("这台主机还不能起草新邮件")
        return self._new_mail_drafts

    def _require_facts(self) -> ConversationalFactService:
        if self._facts is None:
            raise ConversationCapabilityUnavailable("这台主机还没有可用的长期信息存储")
        return self._facts

    async def preflight_fact(
        self, arguments: ConversationOperationArguments, context: PreflightContext
    ) -> str | None:
        """A proposed fact must be storable before anything is written (ADR-0038 §12).

        The key namespace is open, but a credential-shaped key is refused outright, and a
        malformed key is refused with the reason rather than as a store error.
        """
        if isinstance(arguments, FactProposeArguments):
            try:
                validate_fact_key(arguments.key)
            except ForbiddenFactKey as exc:
                return f"「{exc.segment}」看起来是凭证，我不会把它记成长期信息"
            except InvalidFactKey as exc:
                return str(exc)
            try:
                validate_fact_value(arguments.value)
            except DomainError as exc:
                return str(exc)
        return None

    async def _require_new_draft(self, reference: str) -> NewMailDraft:
        drafts = self._require_new_drafts()
        draft = await drafts.get(reference)
        if draft is None:
            from assistant.domain.errors import NewMailDraftNotFound

            raise NewMailDraftNotFound(reference)
        return draft

    async def preflight_new_mail(
        self, arguments: ConversationOperationArguments, context: PreflightContext
    ) -> str | None:
        """A new letter needs an account to send from, and a recipient that resolves.

        Every check here is read-only and runs before the first mutation of the turn, so an unknown
        name, two contacts with one name, no send-ready account or an ambiguous sender leaves the
        runtime exactly as it was (ADR-0037 §10-§14, §28).
        """
        if isinstance(arguments, MailComposeNewArguments):
            resolver = self._recipients
            drafts = self._new_mail_drafts
            if resolver is None or drafts is None:
                return "这台主机还没有配置可以发送邮件的邮箱"
            if not resolver.send_ready:
                return "当前没有可发送邮件的邮箱配置，所以我不能发新邮件"
            current = None
            if arguments.draft_id is not None:
                current = await drafts.get(arguments.draft_id)
                if current is None:
                    return "找不到这份草稿"
            try:
                sender = await resolver.resolve_sender(arguments.sender_account)
                if arguments.recipient_kind is not None:
                    await resolver.resolve_recipient(
                        kind=RecipientSource(arguments.recipient_kind.value),
                        address=arguments.recipient_address,
                        name=arguments.recipient_name,
                        sender=sender,
                    )
                elif current is None:  # pragma: no cover - the arguments require a recipient
                    return "我没有看到收件人"
            except MailRecipientUnresolved as exc:
                return str(exc)
            return None
        if isinstance(arguments, MailPrepareNewSendArguments):
            drafts = self._new_mail_drafts
            if drafts is None:
                return "这台主机还不能起草新邮件"
            if arguments.draft_id is None:
                if ConversationOperationType.MAIL_COMPOSE_NEW in context.preceding:
                    # This turn writes the draft first, so it does not exist yet — and that is fine.
                    return None
                return "我还没有写好这封邮件的草稿"
            return None if await drafts.get(arguments.draft_id) is not None else "找不到这份草稿"
        return None

    async def preflight_contact(
        self, arguments: ConversationOperationArguments, context: PreflightContext
    ) -> str | None:
        """A contact operation must name a contact that exists, before anything else runs."""
        reference = _contact_reference(arguments)
        if reference is None:
            return None
        contacts = self._contacts
        if contacts is None:
            return "这台主机还没有可用的联系人存储"
        try:
            await contacts.require_contact(reference)
        except ContactNotFound:
            return f"找不到这个联系人（{reference[:8]}）"
        except InvalidContact as exc:
            return str(exc)
        return None

    async def _resolve_draft_for_prepare(
        self, asked: MailPrepareReplySendArguments
    ) -> MailDraft:
        """Find the draft to freeze: by id, or the newest draft written for one message."""
        drafts = self._require_drafts()
        if asked.draft_id is not None:
            return (await drafts.get_draft(asked.draft_id)).draft
        message_id = str(asked.message_id)
        matches = [
            listing.draft
            for listing in await drafts.list_drafts(limit=MAIL_LIST_LIMIT)
            if str(listing.draft.reply_to_message_id) == message_id
        ]
        if not matches:
            from assistant.domain.errors import MailDraftNotFound

            raise MailDraftNotFound(f"no reply draft exists for message {message_id}")
        return matches[0]

    def _require_mail(
        self,
    ) -> tuple[MailRepository, MailIntelligenceRepository, MailSendStatusService]:
        if self._mail is None or self._mail_intelligence is None or self._mail_send_status is None:
            raise ConversationCapabilityUnavailable(
                "this host has no mail storage configured"
            )
        return self._mail, self._mail_intelligence, self._mail_send_status

    def _require_drafts(self) -> MailDraftService:
        if self._mail_drafts is None:
            raise ConversationCapabilityUnavailable("reply drafting is not available here")
        return self._mail_drafts

    # ------------------------------------------------------------------------ writes

    async def task_create(self, arguments: ConversationOperationArguments) -> OperationResult:
        created = _expect(TaskCreateArguments, arguments)
        task = await self._tasks.create_task(
            CreateTask(
                title=created.title,
                description=created.description,
                priority=created.priority,
                estimated_minutes=created.estimated_minutes,
                due_at=created.due_at,
            )
        )
        deadline = await self._tasks.get_deadline(task.id)
        return OperationResult(
            kind="task_created",
            ref=str(task.id),
            data={"task": _task_payload(task, deadline, 0)},
        )

    async def task_edit(self, arguments: ConversationOperationArguments) -> OperationResult:
        edited = _expect(TaskEditArguments, arguments)
        current = await self._tasks.require_task(edited.task_id)
        task = await self._tasks.update_task(
            current.id,
            EditTask(
                title=current.title if edited.title is None else edited.title,
                description=current.description,
                priority=current.priority if edited.priority is None else edited.priority,
                estimated_minutes=(
                    current.estimated_minutes
                    if edited.estimated_minutes is None
                    else edited.estimated_minutes
                ),
            ),
        )
        deadline = await self._tasks.get_deadline(task.id)
        return OperationResult(
            kind="task_updated",
            ref=str(task.id),
            data={"task": _task_payload(task, deadline, 0)},
        )

    async def task_set_deadline(self, arguments: ConversationOperationArguments) -> OperationResult:
        set_deadline = _expect(TaskSetDeadlineArguments, arguments)
        task = await self._tasks.require_task(set_deadline.task_id)
        deadline = await self._tasks.set_deadline(task.id, set_deadline.due_at)
        return OperationResult(
            kind="deadline_set",
            ref=str(task.id),
            data={"title": task.title, "due_at": deadline.due_at.isoformat()},
        )

    async def task_clear_deadline(
        self, arguments: ConversationOperationArguments
    ) -> OperationResult:
        clear = _expect(TaskClearDeadlineArguments, arguments)
        task = await self._tasks.require_task(clear.task_id)
        await self._tasks.clear_deadline(task.id)
        return OperationResult(
            kind="deadline_cleared", ref=str(task.id), data={"title": task.title}
        )

    async def task_complete(self, arguments: ConversationOperationArguments) -> OperationResult:
        complete = _expect(TaskCompleteArguments, arguments)
        task = await self._tasks.require_task(complete.task_id)
        await self._tasks.complete_task(task.id)
        return OperationResult(
            kind="task_completed", ref=str(task.id), data={"title": task.title}
        )

    async def calendar_create(self, arguments: ConversationOperationArguments) -> OperationResult:
        created = _expect(CalendarCreateArguments, arguments)
        event = await self._calendar.create_event(
            CreateCalendarEvent(
                title=created.title,
                starts_at=created.starts_at,
                ends_at=created.ends_at,
                description=created.description,
            )
        )
        return OperationResult(
            kind="event_created",
            ref=str(event.id),
            data=_interval_payload(event.title, event.starts_at, event.ends_at),
        )

    async def calendar_recurring_list(
        self, arguments: ConversationOperationArguments
    ) -> OperationResult:
        listed = _expect(CalendarRecurringListArguments, arguments)
        rules = await self._require_recurring().list_rules(
            include_retired=listed.include_retired
        )
        return OperationResult(
            kind="recurring_rules",
            data={
                "rules": [
                    _recurring_payload(rule) for rule in rules[:RECURRING_LIST_LIMIT]
                ],
                "include_retired": listed.include_retired,
            },
        )

    async def calendar_recurring_create_weekly(
        self, arguments: ConversationOperationArguments
    ) -> OperationResult:
        created = _expect(CalendarRecurringCreateWeeklyArguments, arguments)
        rule, was_created = await self._require_recurring().ensure_weekly(
            title=created.title,
            weekday=created.weekday,
            start=created.start_local_time,
            end=created.end_local_time,
            timezone=created.timezone,
            starts_on=created.starts_on,
            ends_on=created.ends_on,
        )
        return OperationResult(
            kind="recurring_rule_created",
            ref=str(rule.id),
            data={**_recurring_payload(rule), "created": was_created},
        )

    async def calendar_recurring_edit(
        self, arguments: ConversationOperationArguments
    ) -> OperationResult:
        edited = _expect(CalendarRecurringEditArguments, arguments)
        rule = await self._require_recurring().edit(
            edited.rule_id,
            title=edited.title,
            weekday=edited.weekday,
            start=edited.start_local_time,
            end=edited.end_local_time,
        )
        return OperationResult(
            kind="recurring_rule_updated",
            ref=str(rule.id),
            data=_recurring_payload(rule),
        )

    async def calendar_recurring_retire(
        self, arguments: ConversationOperationArguments
    ) -> OperationResult:
        retired = _expect(CalendarRecurringRetireArguments, arguments)
        rule = await self._require_recurring().retire(retired.rule_id)
        return OperationResult(
            kind="recurring_rule_retired",
            ref=str(rule.id),
            data=_recurring_payload(rule),
        )

    async def work_record(self, arguments: ConversationOperationArguments) -> OperationResult:
        recorded = _expect(WorkRecordArguments, arguments)
        session = await self._work.record_session(
            task_id=recorded.task_id,
            started_at=recorded.started_at,
            ended_at=recorded.ended_at,
        )
        task = await self._tasks.require_task(session.task_id)
        seconds = int((session.ended_at - session.started_at).total_seconds())
        return OperationResult(
            kind="work_recorded",
            ref=str(session.id),
            data={
                "title": task.title,
                "task_id": str(task.id),
                "minutes": seconds // 60,
                "started_at": session.started_at.isoformat(),
                "ended_at": session.ended_at.isoformat(),
            },
        )

    async def plan_propose_week(self, arguments: ConversationOperationArguments) -> OperationResult:
        proposed = _expect(PlanProposeWeekArguments, arguments)
        detail = await self._planner.create_week_proposal(next_week=proposed.next_week)
        return OperationResult(
            kind="proposal", ref=str(detail.proposal.id), data=_detail(detail)
        )

    async def plan_apply_proposal(
        self, arguments: ConversationOperationArguments
    ) -> OperationResult:
        applied = _expect(PlanApplyProposalArguments, arguments)
        reference = applied.proposal_id
        if reference is None:
            summary = await self._latest_pending_proposal()
            if summary is None:
                return OperationResult(kind="proposal_absent", data={})
            reference = str(summary.proposal.id)
        result = await self._planner.apply_proposal(reference)
        detail = await self._planner.get_proposal_detail(str(result.proposal.id))
        payload = _detail(detail)
        payload.update(
            {
                "applied": True,
                "created_blocks": result.created_blocks,
                "replaced_blocks": result.replaced_blocks,
            }
        )
        return OperationResult(kind="proposal_applied", ref=str(result.proposal.id), data=payload)

    async def notification_read(self, arguments: ConversationOperationArguments) -> OperationResult:
        read = _expect(NotificationReadArguments, arguments)
        notification_id = await self._scheduler.resolve_notification_id(read.notification_id)
        notification = await self._scheduler.mark_notification_read(
            notification_id, at=self._clock.now()
        )
        return OperationResult(
            kind="notification_read",
            ref=str(notification.id),
            data={"title": notification.title},
        )

    # ----------------------------------------------------------------------- helpers

    async def _latest_pending_proposal(self) -> PlanProposalSummary | None:
        summaries = await self._planner.list_proposal_summaries(limit=20)
        for summary in summaries:
            if summary.proposal.status is PlanProposalStatus.PENDING:
                return summary
        return None


def build_phase_10a_registry(handlers: ConversationHandlers) -> ConversationCapabilityRegistry:
    """The frozen Phase 10A capability set, with its code-owned confirmation policies."""
    entries: tuple[tuple[object, ...], ...] = (
        (ConversationOperationType.STATUS_GET, ConfirmationPolicy.READ, handlers.status_get),
        (ConversationOperationType.TASK_LIST, ConfirmationPolicy.READ, handlers.task_list),
        (
            ConversationOperationType.TASK_SHOW,
            ConfirmationPolicy.READ,
            handlers.task_show,
            handlers.preflight_task,
        ),
        (ConversationOperationType.CALENDAR_LIST, ConfirmationPolicy.READ, handlers.calendar_list),
        (
            ConversationOperationType.CALENDAR_RECURRING_LIST,
            ConfirmationPolicy.READ,
            handlers.calendar_recurring_list,
        ),
        (ConversationOperationType.PLAN_CURRENT, ConfirmationPolicy.READ, handlers.plan_current),
        (
            ConversationOperationType.NOTIFICATION_LIST,
            ConfirmationPolicy.READ,
            handlers.notification_list,
        ),
        (
            ConversationOperationType.KNOWLEDGE_ASK,
            ConfirmationPolicy.READ,
            handlers.knowledge_ask,
        ),
        (
            ConversationOperationType.TASK_CREATE,
            ConfirmationPolicy.LOCAL_WRITE,
            handlers.task_create,
        ),
        (
            ConversationOperationType.TASK_EDIT,
            ConfirmationPolicy.LOCAL_WRITE,
            handlers.task_edit,
            handlers.preflight_task,
        ),
        (
            ConversationOperationType.TASK_SET_DEADLINE,
            ConfirmationPolicy.LOCAL_WRITE,
            handlers.task_set_deadline,
            handlers.preflight_task,
        ),
        (
            ConversationOperationType.TASK_CLEAR_DEADLINE,
            ConfirmationPolicy.LOCAL_WRITE,
            handlers.task_clear_deadline,
            handlers.preflight_task,
        ),
        (
            ConversationOperationType.TASK_COMPLETE,
            ConfirmationPolicy.LOCAL_WRITE,
            handlers.task_complete,
            handlers.preflight_task,
        ),
        (
            ConversationOperationType.CALENDAR_CREATE,
            ConfirmationPolicy.LOCAL_WRITE,
            handlers.calendar_create,
        ),
        (
            ConversationOperationType.CALENDAR_RECURRING_CREATE_WEEKLY,
            ConfirmationPolicy.LOCAL_WRITE,
            handlers.calendar_recurring_create_weekly,
            handlers.preflight_recurring,
        ),
        (
            ConversationOperationType.CALENDAR_RECURRING_EDIT,
            ConfirmationPolicy.LOCAL_WRITE,
            handlers.calendar_recurring_edit,
            handlers.preflight_recurring,
        ),
        (
            ConversationOperationType.CALENDAR_RECURRING_RETIRE,
            ConfirmationPolicy.LOCAL_WRITE,
            handlers.calendar_recurring_retire,
            handlers.preflight_recurring,
        ),
        (
            ConversationOperationType.WORK_RECORD,
            ConfirmationPolicy.LOCAL_WRITE,
            handlers.work_record,
            handlers.preflight_task,
        ),
        (
            ConversationOperationType.PLAN_PROPOSE_WEEK,
            ConfirmationPolicy.LOCAL_WRITE,
            handlers.plan_propose_week,
        ),
        (
            ConversationOperationType.PLAN_APPLY_PROPOSAL,
            ConfirmationPolicy.CONFIRM_LOCAL,
            handlers.plan_apply_proposal,
            handlers.preflight_proposal,
        ),
        (
            ConversationOperationType.NOTIFICATION_READ,
            ConfirmationPolicy.LOCAL_WRITE,
            handlers.notification_read,
        ),
        (ConversationOperationType.MAIL_STATUS, ConfirmationPolicy.READ, handlers.mail_status),
        (
            ConversationOperationType.MAIL_LIST,
            ConfirmationPolicy.READ,
            handlers.mail_list,
        ),
        (
            ConversationOperationType.MAIL_SHOW,
            ConfirmationPolicy.READ,
            handlers.mail_show,
            handlers.preflight_mail_message,
        ),
        (
            ConversationOperationType.MAIL_THREAD,
            ConfirmationPolicy.READ,
            handlers.mail_thread,
        ),
        (
            ConversationOperationType.MAIL_RECONCILE_SEND,
            ConfirmationPolicy.READ,
            handlers.mail_reconcile_send,
        ),
        (ConversationOperationType.MAIL_SYNC, ConfirmationPolicy.LOCAL_WRITE, handlers.mail_sync),
        (
            ConversationOperationType.MAIL_REPLY_DRAFT,
            ConfirmationPolicy.LOCAL_WRITE,
            handlers.mail_reply_draft,
            handlers.preflight_mail_message,
        ),
        (
            ConversationOperationType.MAIL_PREPARE_REPLY_SEND,
            ConfirmationPolicy.LOCAL_WRITE,
            handlers.mail_prepare_reply_send,
            handlers.preflight_mail_draft,
        ),
        (
            ConversationOperationType.MAIL_ACCOUNTS,
            ConfirmationPolicy.READ,
            handlers.mail_accounts,
        ),
        (
            ConversationOperationType.MAIL_COMPOSE_NEW,
            ConfirmationPolicy.LOCAL_WRITE,
            handlers.mail_compose_new,
            handlers.preflight_new_mail,
        ),
        (
            ConversationOperationType.MAIL_PREPARE_NEW_SEND,
            ConfirmationPolicy.LOCAL_WRITE,
            handlers.mail_prepare_new_send,
            handlers.preflight_new_mail,
        ),
        (
            ConversationOperationType.CONTACT_LIST,
            ConfirmationPolicy.READ,
            handlers.contact_list,
        ),
        (
            ConversationOperationType.CONTACT_CREATE,
            ConfirmationPolicy.LOCAL_WRITE,
            handlers.contact_create,
        ),
        (
            ConversationOperationType.CONTACT_EDIT,
            ConfirmationPolicy.LOCAL_WRITE,
            handlers.contact_edit,
            handlers.preflight_contact,
        ),
        (
            ConversationOperationType.CONTACT_RETIRE,
            ConfirmationPolicy.LOCAL_WRITE,
            handlers.contact_retire,
            handlers.preflight_contact,
        ),
        (
            ConversationOperationType.FACT_LIST,
            ConfirmationPolicy.READ,
            handlers.fact_list,
        ),
        (
            ConversationOperationType.FACT_SHOW,
            ConfirmationPolicy.READ,
            handlers.fact_show,
        ),
        (
            ConversationOperationType.FACT_PROPOSE,
            ConfirmationPolicy.LOCAL_WRITE,
            handlers.fact_propose,
            handlers.preflight_fact,
        ),
        (
            ConversationOperationType.BRIEF_TODAY,
            ConfirmationPolicy.READ,
            handlers.brief_today,
        ),
        (
            ConversationOperationType.ATTENTION_LIST,
            ConfirmationPolicy.READ,
            handlers.attention_list,
        ),
        (
            ConversationOperationType.ATTENTION_ACKNOWLEDGE,
            ConfirmationPolicy.LOCAL_WRITE,
            handlers.attention_acknowledge,
        ),
        (
            ConversationOperationType.ATTENTION_DISMISS,
            ConfirmationPolicy.LOCAL_WRITE,
            handlers.attention_dismiss,
        ),
        (
            ConversationOperationType.SYSTEM_CAPABILITIES,
            ConfirmationPolicy.READ,
            handlers.system_capabilities,
        ),
    )
    return ConversationCapabilityRegistry(
        ConversationCapability(
            operation_type=cast(ConversationOperationType, entry[0]),
            policy=cast(ConfirmationPolicy, entry[1]),
            handler=cast("OperationHandler", entry[2]),
            preflight=cast("PreflightCheck | None", entry[3] if len(entry) > 3 else None),
        )
        for entry in entries
    )


def _expect[ArgumentsT: ConversationOperationArguments](
    expected: type[ArgumentsT], arguments: ConversationOperationArguments
) -> ArgumentsT:
    """Typed dispatch: a mismatch is a programming error, not a model error."""
    if not isinstance(arguments, expected):
        raise AssertionError(  # pragma: no cover - the registry and the plan agree by construction
            f"handler expected {expected.__name__}, got {type(arguments).__name__}"
        )
    return arguments


def _task_reference(arguments: ConversationOperationArguments) -> TaskId | None:
    """The task an operation names, read through its own type rather than by reflection."""
    if isinstance(
        arguments,
        (
            TaskShowArguments,
            TaskEditArguments,
            TaskCompleteArguments,
            TaskSetDeadlineArguments,
            TaskClearDeadlineArguments,
            WorkRecordArguments,
        ),
    ):
        return arguments.task_id
    return None


def _mail_message_reference(arguments: ConversationOperationArguments) -> str | None:
    """The message an operation names, read through its own type."""
    if isinstance(arguments, (MailShowArguments, MailReplyDraftArguments)):
        return arguments.message_id
    return None


def _recurring_reference(arguments: ConversationOperationArguments) -> str | None:
    """The weekly rule an operation names, read through its own type."""
    if isinstance(
        arguments, (CalendarRecurringEditArguments, CalendarRecurringRetireArguments)
    ):
        return arguments.rule_id
    return None


def _contact_reference(arguments: ConversationOperationArguments) -> str | None:
    """The contact an operation names, read through its own type."""
    if isinstance(arguments, (ContactEditArguments, ContactRetireArguments)):
        return arguments.contact_id
    return None


def _contact_payload(contact: Contact) -> dict[str, object]:
    """One contact as data. Nothing derived, nothing about mail, no credential."""
    return {
        "id": str(contact.id),
        "short_id": str(contact.id)[:8],
        "display_name": contact.display_name,
        "email_address": contact.email_address,
        "status": contact.status.value,
    }


def _fact_payload(
    fact: ConfirmedFact | FactCandidate, *, now: datetime | None = None
) -> dict[str, object]:
    """One candidate or confirmed fact as data: key, value and state, never provenance internals."""
    payload: dict[str, object] = {"key": fact.fact_key, "value": fact.value}
    if isinstance(fact, FactCandidate):
        payload["status"] = fact.status.value
    elif now is not None:
        payload["state"] = fact.state_at(now).value
    return payload


def _attention_payload(summary: AttentionSummary, now: datetime) -> dict[str, object]:
    """One inbox as data: product words only, never a kind constant or a subsystem name."""
    return {
        "total": summary.total,
        "overflow": summary.overflow,
        "by_severity": dict(summary.by_severity),
        "empty": summary.total == 0,
        "items": [
            {
                "id": str(item.id),
                "severity": item.severity.value,
                "status": item.status.value,
                "title": item.title,
                "summary": item.summary,
                "since": item.created_at.isoformat(),
                # How long it has been waiting, decided here because this is where the Clock is.
                # A renderer that read the wall clock would be a second time source (ADR-0039 §2).
                "days_waiting": max(0, (now - item.created_at).days),
            }
            for item in summary.items
        ],
    }


def _brief_payload(brief: TodayBrief) -> dict[str, object]:
    """One brief as data: bounded entries, no ids, no operation names, no database internals."""
    return {
        "local_date": brief.local_date.isoformat(),
        "timezone": brief.timezone,
        "schedule": [_brief_entry(entry) for entry in brief.schedule],
        "tasks": [_brief_entry(entry) for entry in brief.tasks],
        "attention": [_brief_entry(entry) for entry in brief.attention],
        "waiting": [_brief_entry(entry) for entry in brief.waiting],
        "checks": [_brief_entry(entry) for entry in brief.checks],
        "overflow": dict(brief.overflow),
        "empty": brief.is_empty,
    }


def _brief_entry(entry: BriefEntry) -> dict[str, object]:
    """One brief line, with instants as ISO strings for the renderer."""
    return {
        "kind": entry.kind,
        "label": entry.label,
        "detail": entry.detail,
        "starts_at": _iso_or_none(entry.starts_at),
        "ends_at": _iso_or_none(entry.ends_at),
        "at": _iso_or_none(entry.at),
    }


def _iso_or_none(value: object) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _new_mail_draft_payload(
    draft: NewMailDraft, *, created: bool
) -> dict[str, object]:
    """One new-mail draft as data: bounded metadata, and the content the preview will show."""
    return {
        "draft_id": str(draft.id),
        "short_id": str(draft.id)[:8],
        "account_id": draft.account_id,
        "to_address": draft.to_address,
        "subject": draft.subject,
        "body": draft.body_text,
        "version": draft.version,
        "origin": draft.origin.value,
        "created": created,
    }


def _recurring_payload(rule: RecurringCalendarRule) -> dict[str, object]:
    """One weekly rule as data. No fingerprint, no row internals, nothing derived."""
    return {
        "id": str(rule.id),
        "short_id": str(rule.id)[:8],
        "title": rule.title,
        "weekday": rule.weekday,
        "start_local_time": format_clock(rule.start_time),
        "end_local_time": format_clock(rule.end_time),
        "timezone": rule.timezone,
        "starts_on": rule.starts_on.isoformat(),
        "ends_on": None if rule.ends_on is None else rule.ends_on.isoformat(),
        "status": rule.status.value,
    }


def _recurring_occurrence_payload(
    occurrence: RecurringCalendarOccurrence,
) -> dict[str, object]:
    """One derived occurrence as the calendar listing shows it: title, instants, weekday and the
    zone it was derived in. No occurrence id exists to leak."""
    return {
        "title": occurrence.title,
        "starts_at": occurrence.starts_at.isoformat(),
        "ends_at": occurrence.ends_at.isoformat(),
        "weekday": occurrence.starts_at.isoweekday(),
        "timezone": str(occurrence.starts_at.tzinfo),
    }


def _task_payload(
    task: Task, deadline: Deadline | None, actual_seconds: int
) -> dict[str, object]:
    due_at = None if deadline is None else deadline.due_at
    return {
        "id": str(task.id),
        "title": task.title,
        "status": task.status.value,
        "priority": task.priority.value,
        "estimated_minutes": task.estimated_minutes,
        "due_at": None if due_at is None else due_at.isoformat(),
        "actual_seconds": actual_seconds,
        "description": task.description,
    }


def _mail_message_payload(
    message: MailMessage, analysis: MailAnalysis | None
) -> dict[str, object]:
    """One stored message as the conversation may quote it."""
    return {
        "message_id": str(message.id),
        "account_id": message.account_id,
        "subject": message.subject or "(no subject)",
        "from_address": message.from_address,
        "to_addresses": list(message.to_addresses),
        "received_at": _received_at(message),
        "requires_reply": analysis is not None and analysis.requires_reply,
        "category": None if analysis is None else analysis.category.value,
        "summary": None if analysis is None else analysis.summary,
        "thread_id": None,
    }


def _received_at(message: MailMessage) -> str:
    instant = message.sent_at or message.first_seen_at
    return instant.isoformat()


def _interval_payload(title: str, starts_at: datetime, ends_at: datetime) -> dict[str, object]:
    return {"title": title, "starts_at": starts_at.isoformat(), "ends_at": ends_at.isoformat()}


def _next_deadline(
    tasks: list[Task], deadlines: dict[TaskId, Deadline]
) -> dict[str, str] | None:
    open_tasks = [task for task in tasks if task.status is TaskStatus.OPEN]
    pairs = [
        (task, deadlines.get(task.id))
        for task in open_tasks
        if deadlines.get(task.id) is not None
    ]
    if not pairs:
        return None
    task, deadline = min(pairs, key=lambda entry: _due_at(entry[1]))
    return {"title": task.title, "due_at": _due_at(deadline).isoformat()}


def _due_at(deadline: Deadline | None) -> datetime:
    if deadline is None:  # pragma: no cover - callers filter `None` out first
        raise AssertionError("a deadline is required here")
    return deadline.due_at


def _detail(detail: PlanProposalDetail) -> dict[str, object]:
    proposal = detail.proposal
    blocks = detail.blocks
    issues = detail.issues
    return {
        "id": str(proposal.id),
        "window_starts_at": proposal.window.starts_at.isoformat(),
        "window_ends_at": proposal.window.ends_at.isoformat(),
        "timezone": proposal.window.timezone,
        "status": proposal.status.value,
        "block_count": len(blocks),
        "issue_count": len(issues),
        "blocks": [
            _interval_payload(str(block.task_id), block.starts_at, block.ends_at)
            for block in blocks[:10]
        ],
        "issues": [
            {"code": issue.code.value, "message": issue.message} for issue in issues[:5]
        ],
    }


def _proposal_payload(summary: PlanProposalSummary) -> tuple[str, dict[str, object]]:
    proposal = summary.proposal
    return (
        str(proposal.id),
        {
            "id": str(proposal.id),
            "window_starts_at": proposal.window.starts_at.isoformat(),
            "window_ends_at": proposal.window.ends_at.isoformat(),
            "block_count": summary.block_count,
            "issue_count": summary.issue_count,
        },
    )


def _span(evidence: KnowledgeEvidence) -> str:
    span = evidence.source_span
    if span.kind is SourceSpanKind.LINE and span.line_start is not None:
        return f"lines {span.line_start}-{span.line_end}"
    return f"page {span.page_number}"


__all__ = ["ConversationHandlers", "build_phase_10a_registry"]
