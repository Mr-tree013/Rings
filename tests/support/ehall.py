"""Helpers for the eHall tests: a strict fake page and a fake gateway (ADR-0025).

No test opens a browser. The pipeline's rules — service selection, markers, the page contract,
filling only approved values, reading them back, clicking submit exactly once — are all decided by
`nju_certificate.py`, which talks to the narrow `EHallPage` interface, so a fake page is enough to
test every one of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from assistant.adapters.ehall.nju_certificate import (
    RawField,
    RawForm,
)
from assistant.domain.action import ActionRequest, ActionType
from assistant.domain.ehall import (
    CERTIFICATE_SERVICE_NAME,
    EHallFieldDefinition,
    EHallFieldKind,
    EHallFormSnapshot,
)
from assistant.domain.execution import ExecutionOutcome
from assistant.ports.ehall_certificate import EHallSubmissionOutcome

FIELD_KEY = "applicant-name"
SECOND_FIELD_KEY = "certificate-type"
CERTIFICATE_TYPE_OPTIONS = ("在读证明", "成绩证明")


def certificate_snapshot(**overrides: object) -> EHallFormSnapshot:
    """The certificate form as the pipeline describes it, for tests that need no page."""
    values: dict[str, object] = {
        "service_identity": "nju-ehall/证明书申请",
        "fields": (
            EHallFieldDefinition(
                key=FIELD_KEY, label="申请人姓名", kind=EHallFieldKind.TEXT, required=True
            ),
            EHallFieldDefinition(
                key=SECOND_FIELD_KEY,
                label="证明书类型",
                kind=EHallFieldKind.SELECT,
                required=True,
                options=CERTIFICATE_TYPE_OPTIONS,
            ),
        ),
        "required_materials": ("身份证件", "学号"),
        "submit_control": "certificate-submit",
        "page_markers": ("证明书申请 服务说明",),
    }
    values.update(overrides)
    return EHallFormSnapshot(**values)  # type: ignore[arg-type]


def standard_form(**overrides: object) -> RawForm:
    """The certificate form as the live page would report it."""
    values: dict[str, object] = {
        "service_name": CERTIFICATE_SERVICE_NAME,
        "markers": ("证明书申请 服务说明",),
        "materials": ("身份证件", "学号"),
        "fields": (
            RawField(
                key=FIELD_KEY,
                label="申请人姓名",
                kind="text",
                required=True,
            ),
            RawField(
                key=SECOND_FIELD_KEY,
                label="证明书类型",
                kind="select",
                required=True,
                options=("在读证明", "成绩证明"),
            ),
        ),
        "submit_control": "certificate-submit",
    }
    values.update(overrides)
    return RawForm(**values)  # type: ignore[arg-type]


@dataclass
class FakeEHallPage:
    """A scripted `EHallPage`: it records every call and can be made to misbehave."""

    form: RawForm = field(default_factory=standard_form)
    service_error: Exception | None = None
    readback_overrides: dict[str, str] = field(default_factory=dict)
    result: str = "accepted"
    result_error: Exception | None = None
    fill_error: Exception | None = None
    opened: list[str] = field(default_factory=list)
    fills: list[tuple[str, str]] = field(default_factory=list)
    readbacks: list[str] = field(default_factory=list)
    clicks: list[str] = field(default_factory=list)
    closed: bool = False

    async def open_service(self, service_name: str) -> str:
        self.opened.append(service_name)
        if self.service_error is not None:
            raise self.service_error
        return "https://ehall.nju.edu.cn/service"

    async def read_service(self) -> RawForm:
        return self.form

    async def fill(self, definition: EHallFieldDefinition, value: str) -> None:
        if self.fill_error is not None:
            raise self.fill_error
        self.fills.append((definition.key, value))

    async def read_back(self, definition: EHallFieldDefinition) -> str:
        self.readbacks.append(definition.key)
        if definition.key in self.readback_overrides:
            return self.readback_overrides[definition.key]
        for key, value in self.fills:
            if key == definition.key:
                return value
        return ""

    async def click_submit(self, control: str) -> None:
        self.clicks.append(control)
        if self.result_error is not None:
            raise self.result_error

    async def await_result(self, timeout_ms: int) -> str:
        del timeout_ms
        return self.result

    async def close(self) -> None:
        self.closed = True


@dataclass
class FakeEHallSession:
    """A session that opens nothing."""

    started: int = 0
    closed: bool = False

    async def start(self) -> None:
        self.started += 1

    async def close(self) -> None:
        self.closed = True


def gateway_for(
    page: FakeEHallPage,
    *,
    enabled: bool = True,
    session: FakeEHallSession | None = None,
    pages: list[FakeEHallPage] | None = None,
):
    """A real `NjuCertificateGateway` over the fake page, so the policy under test is real."""
    from assistant.adapters.ehall.nju_certificate import NjuCertificateGateway

    opened_session = session or FakeEHallSession()
    queue = pages if pages is not None else [page]
    remaining = list(queue)

    async def _open(session_object: object) -> FakeEHallPage:
        del session_object
        return remaining.pop(0)

    return NjuCertificateGateway(
        lambda: opened_session,  # type: ignore[arg-type,return-value]
        _open,  # type: ignore[arg-type]
        enabled=enabled,
        timeout_seconds=30,
    )


def gateway_over(page: FakeEHallPage, *, enabled: bool = True):
    """A real `NjuCertificateGateway` that always sees the same scripted page.

    Use this when one test performs several reads against one page — a conversation inspects, then
    the executor re-inspects and submits — so `fills`, `readbacks` and `clicks` accumulate in a
    single list and "filled once, clicked once" stays a one-line assertion.
    """
    from assistant.adapters.ehall.nju_certificate import NjuCertificateGateway

    opened_session = FakeEHallSession()

    async def _open(session_object: object) -> FakeEHallPage:
        del session_object
        return page

    return NjuCertificateGateway(
        lambda: opened_session,  # type: ignore[arg-type,return-value]
        _open,  # type: ignore[arg-type]
        enabled=enabled,
        timeout_seconds=30,
    )


@dataclass
class FakeCertificateGateway:
    """A scripted gateway for the service/executor tests: it never touches a page."""

    snapshot: object
    enabled: bool = True
    outcome: EHallSubmissionOutcome = EHallSubmissionOutcome.SUCCEEDED
    summary: str = "the eHall accepted the certificate application"
    error: Exception | None = None
    inspections: int = 0
    submissions: list[tuple[object, str]] = field(default_factory=list)

    async def inspect_form(self):
        self.inspections += 1
        if self.error is not None:
            raise self.error
        return self.snapshot

    async def submit_certificate(self, request, *, expected_fingerprint: str):
        from assistant.ports.ehall_certificate import EHallSubmissionResult

        self.submissions.append((request, expected_fingerprint))
        if self.error is not None:
            raise self.error
        return EHallSubmissionResult(
            outcome=self.outcome,
            summary=self.summary,
            submitted=self.outcome is not EHallSubmissionOutcome.FAILED,
        )

    @property
    def submit_count(self) -> int:
        """How many submissions actually reached the gateway."""
        return len(self.submissions)


class ScriptedEHallExecutor:
    """An `ActionExecutor` for `ehall.submit-certificate` whose outcome the test chooses.

    It records every call, so "submitted exactly once" is an assertion rather than a hope, and no
    browser, no gateway and no Playwright runtime is involved.
    """

    def __init__(self, outcome: ExecutionOutcome | Exception | None = None) -> None:
        self.outcome: ExecutionOutcome | Exception = outcome or ExecutionOutcome.succeeded()
        self.calls: list[ActionRequest] = []

    @property
    def action_type(self) -> ActionType:
        """The one action type this executor handles."""
        return ActionType("ehall.submit-certificate")

    def supports(self, action: ActionRequest) -> bool:
        """A scripted executor can always perform its own action type."""
        return action.action_type == self.action_type

    async def execute(self, action: ActionRequest) -> ExecutionOutcome:
        """Record the call, then return or raise the scripted outcome."""
        self.calls.append(action)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


__all__ = [
    "CERTIFICATE_TYPE_OPTIONS",
    "FIELD_KEY",
    "SECOND_FIELD_KEY",
    "FakeCertificateGateway",
    "FakeEHallPage",
    "FakeEHallSession",
    "ScriptedEHallExecutor",
    "certificate_snapshot",
    "gateway_for",
    "gateway_over",
    "standard_form",
]
