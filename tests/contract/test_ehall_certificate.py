"""The certificate pipeline contract: what it refuses, and what it will never do (ADR-0025).

Driven through the real `NjuCertificateGateway` over a fake page, so every rule the ADR freezes —
exact service selection, the marker check, the page contract, filling only approved values,
reading them back, one submit click, and the difference between a definite failure and an
ambiguous result — is exercised without a browser.
"""

from __future__ import annotations

import pytest

from assistant.adapters.ehall.nju_certificate import (
    PAGE_MARKERS,
    SUBMIT_CONTROL_ID,
    RawField,
    RawForm,
)
from assistant.domain.ehall import (
    CERTIFICATE_SERVICE_NAME,
    EHallCertificateFieldValue,
    EHallCertificatePayload,
    EHallFieldKind,
    EHallFormSnapshot,
)
from assistant.domain.errors import (
    EHallDisabled,
    EHallLoginRequired,
    EHallServiceMismatch,
    EHallUnsupportedRequiredField,
    InvalidEHallForm,
)
from assistant.ports.ehall_certificate import EHallSubmissionOutcome
from tests.support.ehall import (
    FIELD_KEY,
    SECOND_FIELD_KEY,
    FakeEHallPage,
    FakeEHallSession,
    gateway_for,
    standard_form,
)


def _payload(
    snapshot: EHallFormSnapshot,
    *,
    name: str = "张同学",
    certificate_type: str = "在读证明",
) -> EHallCertificatePayload:
    return EHallCertificatePayload(
        fields=(
            EHallCertificateFieldValue(
                key=FIELD_KEY, label="申请人姓名", kind=EHallFieldKind.TEXT, value=name
            ),
            EHallCertificateFieldValue(
                key=SECOND_FIELD_KEY,
                label="证明书类型",
                kind=EHallFieldKind.SELECT,
                value=certificate_type,
            ),
        ),
        page_contract_fingerprint=snapshot.fingerprint(),
        required_materials=snapshot.required_materials,
    )


# -------------------------------------------------------------------- inspection


async def test_inspection_reads_the_contract_and_types_nothing() -> None:
    page = FakeEHallPage()
    session = FakeEHallSession()

    snapshot = await gateway_for(page, session=session).inspect_form()

    assert page.opened == [CERTIFICATE_SERVICE_NAME]
    assert page.fills == []  # inspection is strictly read-only
    assert page.clicks == []
    assert page.closed is True and session.closed is True
    assert [definition.key for definition in snapshot.fields] == [FIELD_KEY, SECOND_FIELD_KEY]
    assert snapshot.required_materials == ("身份证件", "学号")
    assert snapshot.submit_control == SUBMIT_CONTROL_ID
    assert snapshot.page_markers == PAGE_MARKERS


async def test_a_disabled_pipeline_refuses_before_opening_anything() -> None:
    page = FakeEHallPage()
    session = FakeEHallSession()

    with pytest.raises(EHallDisabled):
        await gateway_for(page, enabled=False, session=session).inspect_form()

    assert session.started == 0
    assert page.opened == []


async def test_a_missing_session_is_reported_as_login_required() -> None:
    page = FakeEHallPage(form=standard_form(session_expired=True))

    with pytest.raises(EHallLoginRequired):
        await gateway_for(page).inspect_form()

    assert page.fills == []


async def test_zero_or_several_services_both_fail_closed() -> None:
    for error in (
        EHallServiceMismatch(f"expected exactly one service named {CERTIFICATE_SERVICE_NAME!r}"),
        EHallServiceMismatch("expected exactly one service, found 2"),
    ):
        page = FakeEHallPage(service_error=error)
        with pytest.raises(EHallServiceMismatch):
            await gateway_for(page).inspect_form()


async def test_a_page_without_the_markers_is_not_the_page_we_reviewed() -> None:
    page = FakeEHallPage(form=standard_form(markers=("A different service",)))

    with pytest.raises(EHallServiceMismatch):
        await gateway_for(page).inspect_form()


async def test_a_service_with_another_name_is_refused() -> None:
    page = FakeEHallPage(form=standard_form(service_name="成绩单打印"))

    with pytest.raises(EHallServiceMismatch):
        await gateway_for(page).inspect_form()


async def test_a_required_unsupported_control_blocks_the_pipeline() -> None:
    """A required upload is the user's job; the pipeline will not pretend to fill it."""
    form = standard_form(
        fields=(
            RawField(key=FIELD_KEY, label="申请人姓名", kind="text", required=True),
            RawField(key=None, label="身份证件扫描件", kind="file", required=True),
        )
    )

    with pytest.raises(EHallUnsupportedRequiredField):
        await gateway_for(FakeEHallPage(form=form)).inspect_form()


async def test_an_optional_unsupported_control_is_reported_not_blocking() -> None:
    form = standard_form(
        fields=(
            RawField(key=FIELD_KEY, label="申请人姓名", kind="text", required=True),
            RawField(key=None, label="补充材料", kind="file", required=False),
        )
    )

    snapshot = await gateway_for(FakeEHallPage(form=form)).inspect_form()

    assert [item.label for item in snapshot.unsupported] == ["补充材料"]
    assert snapshot.required_unsupported == ()


async def test_a_field_without_a_stable_name_gets_a_positional_key() -> None:
    form = standard_form(
        fields=(
            RawField(key=None, label="申请人姓名", kind="text", required=True),
            RawField(key=None, label="备注", kind="textarea", required=False),
        ),
        submit_control="certificate-submit",
    )

    snapshot = await gateway_for(FakeEHallPage(form=form)).inspect_form()

    assert [definition.key for definition in snapshot.fields] == ["field-01", "field-02"]


# -------------------------------------------------------------------- submission


async def test_a_successful_submission_fills_reads_back_and_clicks_once() -> None:
    page = FakeEHallPage()
    gateway = gateway_for(page, pages=[page, page])
    snapshot = await gateway.inspect_form()
    payload = _payload(snapshot)

    result = await gateway.submit_certificate(
        payload, expected_fingerprint=snapshot.fingerprint()
    )

    assert result.outcome is EHallSubmissionOutcome.SUCCEEDED
    assert result.submitted is True
    assert page.fills == [(FIELD_KEY, "张同学"), (SECOND_FIELD_KEY, "在读证明")]
    assert page.readbacks == [FIELD_KEY, SECOND_FIELD_KEY]
    assert page.clicks == [SUBMIT_CONTROL_ID]  # exactly one submit click


async def test_the_live_contract_must_still_match_the_approved_one() -> None:
    prepared_page = FakeEHallPage()
    gateway = gateway_for(prepared_page)
    snapshot = await gateway.inspect_form()
    payload = _payload(snapshot)
    changed = FakeEHallPage(
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
    gateway = gateway_for(changed, pages=[changed])

    result = await gateway.submit_certificate(
        payload, expected_fingerprint=snapshot.fingerprint()
    )

    # A changed contract is a *definite* failure: nothing was typed and nothing was submitted.
    assert result.outcome is EHallSubmissionOutcome.FAILED
    assert result.submitted is False
    assert "no longer matches" in result.summary
    assert changed.fills == []
    assert changed.clicks == []


async def test_a_readback_mismatch_stops_before_submitting() -> None:
    page = FakeEHallPage(readback_overrides={FIELD_KEY: "李同学"})
    gateway = gateway_for(page, pages=[page, page])
    snapshot = await gateway.inspect_form()
    payload = _payload(snapshot)

    result = await gateway.submit_certificate(
        payload, expected_fingerprint=snapshot.fingerprint()
    )

    assert result.outcome is EHallSubmissionOutcome.FAILED
    assert result.submitted is False
    assert page.clicks == []
    assert "did not match" in result.summary


async def test_a_value_that_is_no_longer_an_option_is_refused_before_filling() -> None:
    page = FakeEHallPage()
    gateway = gateway_for(page, pages=[page, page])
    snapshot = await gateway.inspect_form()
    payload = _payload(snapshot, certificate_type="毕业证明")

    result = await gateway.submit_certificate(
        payload, expected_fingerprint=snapshot.fingerprint()
    )

    assert result.outcome is EHallSubmissionOutcome.FAILED
    assert page.fills == [] and page.clicks == []


async def test_a_page_that_refuses_the_submission_is_a_failure() -> None:
    page = FakeEHallPage(result="rejected")
    gateway = gateway_for(page, pages=[page, page])
    snapshot = await gateway.inspect_form()

    result = await gateway.submit_certificate(
        _payload(snapshot), expected_fingerprint=snapshot.fingerprint()
    )

    assert result.outcome is EHallSubmissionOutcome.FAILED
    assert result.submitted is True  # the click happened; the university said no
    assert page.clicks == [SUBMIT_CONTROL_ID]


async def test_an_inconclusive_result_is_unknown_not_success() -> None:
    page = FakeEHallPage(result="unknown")
    gateway = gateway_for(page, pages=[page, page])
    snapshot = await gateway.inspect_form()

    result = await gateway.submit_certificate(
        _payload(snapshot), expected_fingerprint=snapshot.fingerprint()
    )

    assert result.outcome is EHallSubmissionOutcome.UNKNOWN
    assert "did not show a decisive result" in result.summary


async def test_a_crash_after_the_click_is_unknown() -> None:
    page = FakeEHallPage(result_error=RuntimeError("browser died"))
    gateway = gateway_for(page, pages=[page, page])
    snapshot = await gateway.inspect_form()

    result = await gateway.submit_certificate(
        _payload(snapshot), expected_fingerprint=snapshot.fingerprint()
    )

    assert result.outcome is EHallSubmissionOutcome.UNKNOWN
    assert result.submitted is True
    assert page.clicks == [SUBMIT_CONTROL_ID]


async def test_a_failure_before_the_click_is_definite() -> None:
    page = FakeEHallPage(fill_error=RuntimeError("the field is gone"))
    gateway = gateway_for(page, pages=[page, page])
    snapshot = await gateway.inspect_form()

    result = await gateway.submit_certificate(
        _payload(snapshot), expected_fingerprint=snapshot.fingerprint()
    )

    assert result.outcome is EHallSubmissionOutcome.FAILED
    assert result.submitted is False
    assert page.clicks == []


async def test_a_session_that_expired_during_execution_fails_before_the_click() -> None:
    page = FakeEHallPage()
    gateway = gateway_for(page, pages=[page])
    prepared_page = FakeEHallPage()
    snapshot = await gateway_for(prepared_page).inspect_form()
    expired = FakeEHallPage(form=standard_form(session_expired=True))
    gateway = gateway_for(expired, pages=[expired])

    result = await gateway.submit_certificate(
        _payload(snapshot), expected_fingerprint=snapshot.fingerprint()
    )

    # The session expired before anything was typed, so this is a definite failure that the user
    # can fix by logging in again and approving afresh.
    assert result.outcome is EHallSubmissionOutcome.FAILED
    assert result.submitted is False
    assert "pw ehall login" in result.summary
    assert expired.clicks == []
    assert expired.fills == []


async def test_the_pipeline_only_ever_opens_the_whitelisted_service() -> None:
    page = FakeEHallPage()
    gateway = gateway_for(page, pages=[page, page])
    snapshot = await gateway.inspect_form()
    await gateway.submit_certificate(
        _payload(snapshot), expected_fingerprint=snapshot.fingerprint()
    )

    assert page.opened == [CERTIFICATE_SERVICE_NAME, CERTIFICATE_SERVICE_NAME]
    assert all(name == CERTIFICATE_SERVICE_NAME for name in page.opened)


async def test_a_form_with_no_fillable_field_is_refused() -> None:
    """A form this pipeline cannot fill is not a form it will pretend to complete."""
    form = RawForm(
        service_name=CERTIFICATE_SERVICE_NAME,
        markers=("证明书申请",),
        fields=(RawField(key=None, label="扫描件", kind="file", required=False),),
        submit_control=SUBMIT_CONTROL_ID,
    )

    with pytest.raises(InvalidEHallForm):
        await gateway_for(FakeEHallPage(form=form)).inspect_form()
