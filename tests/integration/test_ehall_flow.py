"""The certificate errand end to end: prepare → approve → execute (ADR-0025).

Real SQLite, the real Phase 6A approval and execution boundary, the real prepare service, the real
pipeline policy over a fake page, and a scripted gateway. What is under test is the chain a user
walks and the refusals that keep it safe: nothing is typed during preparation, the approved payload
is exactly the preview, execution needs a consumed approval, a changed page stops before typing,
and a tampered action never reaches the browser.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from assistant.adapters.ehall.executor import EHallCertificateExecutor
from assistant.adapters.ehall.nju_certificate import (
    NjuCertificateGateway,
    RawField,
)
from assistant.application.action_execution import ActionExecutionService
from assistant.application.action_service import ActionService
from assistant.application.approval_service import ApprovalService
from assistant.application.case_service import CaseService
from assistant.application.ehall_certificate import EHallCertificateService
from assistant.domain.action import ActionRequestStatus
from assistant.domain.case import Case
from assistant.domain.ehall import (
    EHallCertificatePayload,
)
from assistant.domain.errors import (
    ActionExecutionUnresolved,
    ApprovalUnavailable,
    CapabilityUnavailable,
    CaseNotOpen,
    EHallDisabled,
    EHallUnsupportedRequiredField,
    InvalidEHallForm,
)
from assistant.domain.execution import ExecutionRunStatus
from assistant.ports.ehall_certificate import EHallSubmissionOutcome
from assistant.store.db import Database
from assistant.store.errors import CommitmentStoreError
from assistant.store.migrations import apply_migrations
from tests.support.actions import SECRET_TOKEN, ActionStores, FixedTokenFactory
from tests.support.ehall import (
    FIELD_KEY,
    SECOND_FIELD_KEY,
    FakeCertificateGateway,
    FakeEHallPage,
    standard_form,
)
from tests.support.fakes import FakeClock

NOW = datetime(2026, 9, 24, 9, 0, tzinfo=UTC)


class _Session:
    """A session double: the pipeline's policy is what these tests exercise."""

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None


def _gateway(page: object, *, enabled: bool = True) -> NjuCertificateGateway:
    """The real pipeline over one fake page."""
    return NjuCertificateGateway(
        lambda: _Session(),  # type: ignore[arg-type,return-value]
        _page_factory(page),
        enabled=enabled,
        timeout_seconds=30,
    )


def _page_factory(page: object):
    async def _open(session: object) -> object:
        del session
        return page

    return _open


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=NOW)


@pytest.fixture
def database(tmp_path: Path, clock: FakeClock) -> Database:
    db = Database.at(tmp_path / "assistant.db")
    apply_migrations(db, clock=clock)
    return db


@pytest.fixture
def stores(database: Database, clock: FakeClock) -> ActionStores:
    return ActionStores(database, clock)


def _service(
    stores: ActionStores, clock: FakeClock, gateway: object, *, enabled: bool = True
) -> EHallCertificateService:
    return EHallCertificateService(
        gateway,  # type: ignore[arg-type]
        stores.cases,
        stores.actions,
        clock,
        enabled=enabled,
    )


async def _open_case(stores: ActionStores, clock: FakeClock) -> Case:
    return await CaseService(stores.cases, stores.actions, clock).create_case(
        "Apply for a certificate"
    )


def _values() -> dict[str, str]:
    return {FIELD_KEY: "张同学", SECOND_FIELD_KEY: "在读证明"}


async def _snapshot(page: FakeEHallPage | None = None):
    """The live contract, as the real pipeline would derive it from a page."""
    return await _gateway(page or FakeEHallPage()).inspect_form()


async def _approved(
    stores: ActionStores, clock: FakeClock, action_id
) -> None:
    service = ApprovalService(
        stores.actions, clock, token_factory=FixedTokenFactory(SECRET_TOKEN)
    )
    issued = await service.create_challenge(action_id)
    await service.approve(action_id, issued.token)


# ---------------------------------------------------------------------- prepare


async def test_preparing_freezes_the_contract_and_submits_nothing(
    stores: ActionStores, clock: FakeClock
) -> None:
    snapshot = await _snapshot()
    gateway = FakeCertificateGateway(snapshot=snapshot)
    case = await _open_case(stores, clock)

    preparation = await _service(stores, clock, gateway).prepare(
        case_id=case.id, field_values=_values()
    )

    payload = EHallCertificatePayload.from_payload(preparation.action.payload)
    assert preparation.action.action_type.value == "ehall.submit-certificate"
    assert preparation.action.status is ActionRequestStatus.PREPARED
    assert payload.page_contract_fingerprint == snapshot.fingerprint()
    assert payload.service_identity == snapshot.service_identity
    assert [(item.key, item.value) for item in payload.fields] == [
        (FIELD_KEY, "张同学"),
        (SECOND_FIELD_KEY, "在读证明"),
    ]
    # The preview the user approves *is* the payload, field for field.
    assert preparation.preview.fields == payload.fields
    assert preparation.preview.consequence == payload.consequence
    assert preparation.preview.required_materials == snapshot.required_materials
    assert gateway.inspections == 1
    assert gateway.submit_count == 0


async def test_prepare_refuses_values_the_form_cannot_accept(
    stores: ActionStores, clock: FakeClock
) -> None:
    snapshot = await _snapshot()
    gateway = FakeCertificateGateway(snapshot=snapshot)
    case = await _open_case(stores, clock)
    service = _service(stores, clock, gateway)

    with pytest.raises(InvalidEHallForm):  # a required field is missing
        await service.prepare(case_id=case.id, field_values={FIELD_KEY: "张同学"})
    with pytest.raises(InvalidEHallForm):  # not one of the offered options
        await service.prepare(
            case_id=case.id,
            field_values={**_values(), SECOND_FIELD_KEY: "毕业证明"},
        )
    with pytest.raises(InvalidEHallForm):  # a key the form does not have
        await service.prepare(case_id=case.id, field_values={**_values(), "nickname": "x"})
    with pytest.raises(InvalidEHallForm):  # a blank value is not a value
        await service.prepare(
            case_id=case.id, field_values={**_values(), FIELD_KEY: "   "}
        )
    assert await stores.actions.count_actions() == 0


async def test_prepare_needs_an_open_case_and_an_enabled_pipeline(
    stores: ActionStores, clock: FakeClock
) -> None:
    snapshot = await _snapshot()
    gateway = FakeCertificateGateway(snapshot=snapshot)
    cases = CaseService(stores.cases, stores.actions, clock)
    case = await _open_case(stores, clock)
    await cases.complete_case(case.id)

    with pytest.raises(CaseNotOpen):
        await _service(stores, clock, gateway).prepare(
            case_id=case.id, field_values=_values()
        )
    with pytest.raises(EHallDisabled):
        await _service(stores, clock, gateway, enabled=False).prepare(
            case_id=case.id, field_values=_values()
        )
    assert await stores.actions.count_actions() == 0


async def test_a_required_upload_blocks_preparation(
    stores: ActionStores, clock: FakeClock
) -> None:
    """A required scan is the user's job; the pipeline says so instead of faking it."""
    case = await _open_case(stores, clock)
    blocking = _gateway(
        FakeEHallPage(
            form=standard_form(
                fields=(
                    RawField(key=FIELD_KEY, label="申请人姓名", kind="text", required=True),
                    RawField(key=None, label="身份证件扫描件", kind="file", required=True),
                )
            )
        )
    )

    with pytest.raises(EHallUnsupportedRequiredField):
        await _service(stores, clock, blocking).prepare(
            case_id=case.id, field_values={FIELD_KEY: "张同学"}
        )
    assert await stores.actions.count_actions() == 0


# --------------------------------------------------------------------- approval


async def test_the_whole_chain_from_case_to_submission(
    stores: ActionStores, clock: FakeClock
) -> None:
    snapshot = await _snapshot()
    gateway = FakeCertificateGateway(snapshot=snapshot)
    case = await _open_case(stores, clock)
    preparation = await _service(stores, clock, gateway).prepare(
        case_id=case.id, field_values=_values()
    )

    await _approved(stores, clock, preparation.action.id)

    executor = EHallCertificateExecutor(gateway)
    assert executor.supports(preparation.action) is True
    result = await ActionExecutionService(
        stores.actions, {executor.action_type: executor}, clock
    ).execute(preparation.action.id)

    assert result.status is ExecutionRunStatus.SUCCEEDED
    assert result.action_status == "executed"
    assert gateway.submit_count == 1
    action, expected_fingerprint = gateway.submissions[0]
    assert expected_fingerprint == preparation.preview.page_contract_fingerprint
    assert action.to_payload() == preparation.action.payload  # the approved payload, unchanged
    stored = await stores.actions.get_action(preparation.action.id)
    assert stored is not None and stored.status is ActionRequestStatus.EXECUTED


async def test_execution_without_an_approval_never_reaches_the_gateway(
    stores: ActionStores, clock: FakeClock
) -> None:
    snapshot = await _snapshot()
    gateway = FakeCertificateGateway(snapshot=snapshot)
    case = await _open_case(stores, clock)
    preparation = await _service(stores, clock, gateway).prepare(
        case_id=case.id, field_values=_values()
    )
    executor = EHallCertificateExecutor(gateway)

    with pytest.raises(ApprovalUnavailable):
        await ActionExecutionService(
            stores.actions, {executor.action_type: executor}, clock
        ).execute(preparation.action.id)

    assert gateway.submit_count == 0
    assert await stores.actions.count_executions(preparation.action.id) == 0


# --------------------------------------------------------------- execution safety


async def test_a_changed_page_stops_before_anything_is_typed(
    stores: ActionStores, clock: FakeClock
) -> None:
    """The user approved contract A; the live page is contract B. Nothing happens."""
    prepared = await _snapshot()
    case = await _open_case(stores, clock)
    preparation = await _service(
        stores, clock, FakeCertificateGateway(snapshot=prepared)
    ).prepare(case_id=case.id, field_values=_values())
    await _approved(stores, clock, preparation.action.id)
    changed_page = FakeEHallPage(
        form=standard_form(
            fields=(
                RawField(key=FIELD_KEY, label="申请人姓名", kind="text", required=True),
                RawField(
                    key=SECOND_FIELD_KEY,
                    label="证明书类型",
                    kind="select",
                    required=True,
                    options=("在读证明", "成绩证明", "毕业证明"),
                ),
            )
        )
    )
    executor = EHallCertificateExecutor(_gateway(changed_page))

    result = await ActionExecutionService(
        stores.actions, {executor.action_type: executor}, clock
    ).execute(preparation.action.id)

    assert result.status is ExecutionRunStatus.FAILED
    assert changed_page.fills == []
    assert changed_page.clicks == []


async def test_a_tampered_action_never_reaches_the_browser(
    stores: ActionStores, clock: FakeClock, database: Database
) -> None:
    snapshot = await _snapshot()
    gateway = FakeCertificateGateway(snapshot=snapshot)
    case = await _open_case(stores, clock)
    preparation = await _service(stores, clock, gateway).prepare(
        case_id=case.id, field_values=_values()
    )
    await _approved(stores, clock, preparation.action.id)
    with database.connect() as connection:
        connection.execute(
            "UPDATE action_requests SET payload_json = ? WHERE id = ?",
            ('{"schema_version":1}', str(preparation.action.id)),
        )
    executor = EHallCertificateExecutor(gateway)

    with pytest.raises((CommitmentStoreError, InvalidEHallForm)):
        await ActionExecutionService(
            stores.actions, {executor.action_type: executor}, clock
        ).execute(preparation.action.id)

    assert gateway.submit_count == 0
    assert await stores.actions.count_executions(preparation.action.id) == 0


async def test_two_concurrent_executions_submit_once(
    stores: ActionStores, clock: FakeClock
) -> None:
    snapshot = await _snapshot()
    gateway = FakeCertificateGateway(snapshot=snapshot)
    case = await _open_case(stores, clock)
    preparation = await _service(stores, clock, gateway).prepare(
        case_id=case.id, field_values=_values()
    )
    await _approved(stores, clock, preparation.action.id)
    executor = EHallCertificateExecutor(gateway)
    service = ActionExecutionService(
        stores.actions, {executor.action_type: executor}, clock
    )

    results = await asyncio.gather(
        service.execute(preparation.action.id),
        service.execute(preparation.action.id),
        return_exceptions=True,
    )

    winners = [item for item in results if not isinstance(item, BaseException)]
    losers = [item for item in results if isinstance(item, BaseException)]
    assert len(winners) == 1
    assert len(losers) == 1
    assert gateway.submit_count == 1
    assert await stores.actions.count_executions(preparation.action.id) == 1


async def test_an_unknown_submission_blocks_a_second_attempt(
    stores: ActionStores, clock: FakeClock
) -> None:
    snapshot = await _snapshot()
    gateway = FakeCertificateGateway(
        snapshot=snapshot,
        outcome=EHallSubmissionOutcome.UNKNOWN,
        summary="the result could not be determined",
    )
    case = await _open_case(stores, clock)
    preparation = await _service(stores, clock, gateway).prepare(
        case_id=case.id, field_values=_values()
    )
    await _approved(stores, clock, preparation.action.id)
    executor = EHallCertificateExecutor(gateway)
    service = ActionExecutionService(
        stores.actions, {executor.action_type: executor}, clock
    )

    first = await service.execute(preparation.action.id)

    assert first.status is ExecutionRunStatus.UNKNOWN
    stored = await stores.actions.get_action(preparation.action.id)
    assert stored is not None and stored.status is ActionRequestStatus.PREPARED
    with pytest.raises((ActionExecutionUnresolved, ApprovalUnavailable)):
        await service.execute(preparation.action.id)
    assert gateway.submit_count == 1  # never submitted twice


async def test_a_disabled_gateway_costs_nothing(
    stores: ActionStores, clock: FakeClock
) -> None:
    snapshot = await _snapshot()
    case = await _open_case(stores, clock)
    preparation = await _service(
        stores, clock, FakeCertificateGateway(snapshot=snapshot)
    ).prepare(case_id=case.id, field_values=_values())
    disabled = FakeCertificateGateway(snapshot=snapshot, enabled=False)
    executor = EHallCertificateExecutor(disabled)
    await _approved(stores, clock, preparation.action.id)

    assert executor.supports(preparation.action) is False
    with pytest.raises(CapabilityUnavailable):
        await ActionExecutionService(
            stores.actions, {executor.action_type: executor}, clock
        ).execute(preparation.action.id)

    assert disabled.submit_count == 0
    overview = await ActionService(stores.actions, clock).overview(preparation.action.id)
    assert overview.approval_state.value == "valid"  # untouched
