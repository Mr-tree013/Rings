"""Helpers for the Phase 7B tests: a real successful execution, real stores, scripted validators.

A playbook candidate can only come from a *definitive* success, so the fixture has to produce one
honestly: a real case, a real immutable action, a real challenge, a real approval, and a real
execution run — driven by a scripted executor, because the point of these tests is the review
pipeline, not SMTP or a browser.

The scripted executors and gateways double as spies: after a dry run they must show **zero** calls,
which is how "replay never performs a side effect" is proven rather than asserted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from assistant.application.action_execution import ActionExecutionService
from assistant.application.approval_service import ApprovalService
from assistant.application.case_service import CaseService
from assistant.application.playbook_replay import PlaybookReplayRegistry
from assistant.application.playbook_service import PlaybookService
from assistant.domain.action import ActionRequest, ActionRequestStatus, ActionType
from assistant.domain.ehall import (
    EHallCertificateFieldValue,
    EHallCertificatePayload,
    EHallFieldKind,
)
from assistant.domain.execution import ExecutionRun, ExecutionRunStatus
from assistant.domain.mail_send import MailSendPayload
from assistant.domain.playbook import ReplayTestStatus, ReplayValidationResult
from assistant.store.actions import SqliteActionRepository
from assistant.store.cases import SqliteCaseRepository
from assistant.store.db import Database
from assistant.store.playbooks import SqlitePlaybookRepository
from tests.support.actions import FakeActionExecutor, FixedTokenFactory
from tests.support.fakes import FakeClock

MAIL_SEND_ACTION_TYPE = ActionType("mail.send")
EHALL_ACTION_TYPE = ActionType("ehall.submit-certificate")
FUTURE_ACTION_TYPE = ActionType("example.future-action")
"""An action type that executes fine and has no replay validator (ADR-0028 §38)."""

NOW = datetime(2026, 9, 24, 9, 0, tzinfo=UTC)

PAGE_CONTRACT_FINGERPRINT = "c" * 64
MESSAGE_ID = "<approved-2026-09-24@example.edu>"


def mail_send_payload(
    *, body_text: str = "Dear Ada, I will submit the report on Friday."
) -> dict[str, object]:
    """One exact, structurally valid `mail.send` document."""
    return MailSendPayload(
        draft_id=uuid4(),
        draft_version=3,
        account_id="smail",
        from_address="student@example.edu",
        to_addresses=("ada@example.edu",),
        subject="Re: SE lab deadline",
        body_text=body_text,
        rfc_message_id=MESSAGE_ID,
        date_header="Thu, 24 Sep 2026 09:00:00 +0000",
        in_reply_to_header="<original@example.edu>",
        references=("<original@example.edu>",),
    ).to_payload()


def ehall_payload() -> dict[str, object]:
    """One exact, structurally valid `ehall.submit-certificate` document."""
    return EHallCertificatePayload(
        fields=(
            EHallCertificateFieldValue(
                key="applicant-name",
                label="申请人姓名",
                kind=EHallFieldKind.TEXT,
                value="张同学",
            ),
            EHallCertificateFieldValue(
                key="certificate-type",
                label="证明书类型",
                kind=EHallFieldKind.SELECT,
                value="在读证明",
            ),
        ),
        page_contract_fingerprint=PAGE_CONTRACT_FINGERPRINT,
        required_materials=("学生证",),
    ).to_payload()


def payload_for(action_type: ActionType) -> dict[str, object]:
    """A structurally valid payload for one capability, for tests that parametrise over them."""
    if action_type == MAIL_SEND_ACTION_TYPE:
        return mail_send_payload()
    if action_type == EHALL_ACTION_TYPE:
        return ehall_payload()
    return {"note": "an action type this project does not implement"}


class RecordingReplayValidator:
    """A validator that records what it was handed and can be scripted to fail."""

    def __init__(
        self,
        action_type: ActionType = MAIL_SEND_ACTION_TYPE,
        *,
        contract_version: int = 1,
        result: ReplayValidationResult | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._action_type = action_type
        self._contract_version = contract_version
        self.result = result if result is not None else ReplayValidationResult.pass_()
        self.error = error
        self.calls: list[ActionRequest] = []

    @property
    def action_type(self) -> ActionType:
        return self._action_type

    @property
    def contract_version(self) -> int:
        return self._contract_version

    def validate(self, action: ActionRequest) -> ReplayValidationResult:
        self.calls.append(action)
        if self.error is not None:
            raise self.error
        return self.result

    def script_failure(self, *issue_codes: str) -> RecordingReplayValidator:
        self.result = ReplayValidationResult(
            status=ReplayTestStatus.FAILED, issue_codes=tuple(issue_codes)
        )
        return self


def default_registry(
    *validators: object,
) -> PlaybookReplayRegistry:
    """A registry over the given validators, or the production pair when none are given."""
    if not validators:
        return PlaybookReplayRegistry.default()
    return PlaybookReplayRegistry(validators)  # type: ignore[arg-type]


@dataclass
class PlaybookHarness:
    """Real stores plus everything needed to produce a definitively successful action."""

    database: Database
    clock: FakeClock = field(default_factory=lambda: FakeClock(start=NOW))
    tokens: FixedTokenFactory = field(default_factory=FixedTokenFactory)
    cases: SqliteCaseRepository = field(init=False)
    actions: SqliteActionRepository = field(init=False)
    playbooks: SqlitePlaybookRepository = field(init=False)

    def __post_init__(self) -> None:
        self.cases = SqliteCaseRepository(self.database)
        self.actions = SqliteActionRepository(self.database)
        self.playbooks = SqlitePlaybookRepository(self.database)

    # ------------------------------------------------------------------ sources

    async def prepare(
        self,
        action_type: ActionType = MAIL_SEND_ACTION_TYPE,
        payload: object | None = None,
    ) -> ActionRequest:
        """A stored, PREPARED action inside a new case."""
        service = CaseService(self.cases, self.actions, self.clock)
        case = await service.create_case("A piece of work that succeeded")
        return await service.prepare_action(
            case.id, action_type, payload if payload is not None else payload_for(action_type)
        )

    async def succeed(
        self, action: ActionRequest
    ) -> tuple[ActionRequest, ExecutionRun, FakeActionExecutor]:
        """Approve and execute one action for real, returning the EXECUTED action and its run."""
        approvals = ApprovalService(
            self.actions, self.clock, token_factory=self.tokens
        )
        issued = await approvals.create_challenge(action.id)
        await approvals.approve(action.id, issued.token)
        executor = FakeActionExecutor(action_type=action.action_type).script_success()
        execution = ActionExecutionService(
            self.actions, {executor.action_type: executor}, self.clock
        )
        result = await execution.execute(action.id)
        stored = await self.actions.get_action(action.id)
        assert stored is not None
        assert stored.status is ActionRequestStatus.EXECUTED
        assert result.run.status is ExecutionRunStatus.SUCCEEDED
        return stored, result.run, executor

    async def executed(
        self,
        action_type: ActionType = MAIL_SEND_ACTION_TYPE,
        payload: object | None = None,
    ) -> tuple[ActionRequest, ExecutionRun, FakeActionExecutor]:
        """A definitive success in one call: the ordinary starting point for these tests."""
        return await self.succeed(await self.prepare(action_type, payload))

    # ------------------------------------------------------------------ services

    def service(self, *, replay: PlaybookReplayRegistry | None = None) -> PlaybookService:
        """The real service over the real stores, with an injectable replay capability set."""
        return PlaybookService(
            self.playbooks,
            self.actions,
            self.clock,
            replay=replay if replay is not None else default_registry(),
        )


__all__ = [
    "EHALL_ACTION_TYPE",
    "FUTURE_ACTION_TYPE",
    "MAIL_SEND_ACTION_TYPE",
    "MESSAGE_ID",
    "NOW",
    "PAGE_CONTRACT_FINGERPRINT",
    "PlaybookHarness",
    "RecordingReplayValidator",
    "default_registry",
    "ehall_payload",
    "mail_send_payload",
    "payload_for",
]
