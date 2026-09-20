"""The certificate form contract, payload and validation rules (ADR-0025)."""

from __future__ import annotations

import pytest

from assistant.domain.ehall import (
    CERTIFICATE_CONSEQUENCE,
    CERTIFICATE_SERVICE_IDENTITY,
    EHallCertificateFieldValue,
    EHallCertificatePayload,
    EHallFieldDefinition,
    EHallFieldKind,
    EHallFormSnapshot,
    EHallUnsupportedControl,
    build_field_values,
    page_contract_fingerprint,
)
from assistant.domain.errors import InvalidEHallForm


def _field(**overrides: object) -> EHallFieldDefinition:
    values: dict[str, object] = {
        "key": "applicant-name",
        "label": "申请人姓名",
        "kind": EHallFieldKind.TEXT,
        "required": True,
    }
    values.update(overrides)
    return EHallFieldDefinition(**values)  # type: ignore[arg-type]


def _snapshot(**overrides: object) -> EHallFormSnapshot:
    values: dict[str, object] = {
        "service_identity": CERTIFICATE_SERVICE_IDENTITY,
        "fields": (
            _field(),
            _field(
                key="certificate-type",
                label="证明书类型",
                kind=EHallFieldKind.SELECT,
                options=("在读证明", "成绩证明"),
            ),
        ),
        "required_materials": ("身份证件",),
        "submit_control": "certificate-submit",
        "page_markers": ("证明书申请",),
    }
    values.update(overrides)
    return EHallFormSnapshot(**values)  # type: ignore[arg-type]


def test_a_field_labels_are_normalised_and_bounded() -> None:
    field = _field(label="  申请人   姓名 ")

    assert field.label == "申请人 姓名"
    assert field.required is True
    for overrides in (
        {"key": "1bad"},
        {"key": "with space"},
        {"key": ""},
        {"label": "   "},
        {"label": "x" * 201},
    ):
        with pytest.raises(InvalidEHallForm):
            _field(**overrides)


def test_only_choice_controls_carry_options() -> None:
    assert _field(kind=EHallFieldKind.TEXTAREA).options == ()
    with pytest.raises(InvalidEHallForm):
        _field(kind=EHallFieldKind.SELECT)
    with pytest.raises(InvalidEHallForm):
        _field(kind=EHallFieldKind.RADIO)
    with pytest.raises(InvalidEHallForm):
        _field(options=("only-for-choices",))


def test_a_snapshot_needs_a_usable_shape() -> None:
    snapshot = _snapshot()

    assert [item.key for item in snapshot.fields] == [
        "applicant-name",
        "certificate-type",
    ]
    for overrides in (
        {"service_identity": "  "},
        {"fields": ()},
        {"submit_control": ""},
        {"required_materials": tuple(f"m{index}" for index in range(21))},
        {"fields": (_field(), _field())},
    ):
        with pytest.raises(InvalidEHallForm):
            _snapshot(**overrides)


def test_a_required_unsupported_control_blocks_the_pipeline() -> None:
    snapshot = _snapshot(
        unsupported=(
            EHallUnsupportedControl(
                label="身份证件扫描件", description="unsupported control type 'file'", required=True
            ),
        )
    )

    assert snapshot.required_unsupported
    with pytest.raises(Exception, match="cannot fill"):
        snapshot.require_supported()


def test_an_optional_unsupported_control_does_not_block() -> None:
    snapshot = _snapshot(
        unsupported=(
            EHallUnsupportedControl(
                label="补充材料", description="unsupported control type 'file'", required=False
            ),
        )
    )

    snapshot.require_supported()  # does not raise
    assert snapshot.required_unsupported == ()


def test_the_page_contract_fingerprint_covers_order_and_content() -> None:
    baseline = _snapshot().fingerprint()

    assert baseline == _snapshot().fingerprint()
    assert len(baseline) == 64
    # Reordering the fields is a different contract: the user reviewed the first order.
    reversed_fields = _snapshot(
        fields=(
            _field(
                key="certificate-type",
                label="证明书类型",
                kind=EHallFieldKind.SELECT,
                options=("在读证明", "成绩证明"),
            ),
            _field(),
        )
    )
    assert reversed_fields.fingerprint() != baseline
    for changed in (
        _snapshot(required_materials=("身份证件", "学号")),
        _snapshot(submit_control="other-submit"),
        _snapshot(page_markers=("证明书申请", "其它")),
        _snapshot(
            fields=(
                _field(label="姓名"),
                _field(
                    key="certificate-type",
                    label="证明书类型",
                    kind=EHallFieldKind.SELECT,
                    options=("在读证明", "成绩证明"),
                ),
            )
        ),
        _snapshot(
            fields=(
                _field(),
                _field(
                    key="certificate-type",
                    label="证明书类型",
                    kind=EHallFieldKind.SELECT,
                    options=("在读证明", "成绩证明", "毕业证明"),
                ),
            )
        ),
    ):
        assert changed.fingerprint() != baseline


def test_the_fingerprint_helper_matches_the_snapshot() -> None:
    snapshot = _snapshot()

    assert page_contract_fingerprint(
        pipeline_id=snapshot.pipeline_id,
        page_contract_version=snapshot.page_contract_version,
        service_identity=snapshot.service_identity,
        page_markers=snapshot.page_markers,
        fields=snapshot.fields,
        required_materials=snapshot.required_materials,
        submit_control=snapshot.submit_control,
    ) == snapshot.fingerprint()


def test_field_values_come_only_from_what_the_user_supplied() -> None:
    snapshot = _snapshot()

    values = build_field_values(
        snapshot, {"applicant-name": " 张同学 ", "certificate-type": "在读证明"}
    )

    assert [(item.key, item.value) for item in values] == [
        ("applicant-name", "张同学"),
        ("certificate-type", "在读证明"),
    ]
    for supplied, expected in (
        ({"applicant-name": "张同学"}, "required"),
        ({"applicant-name": "张", "certificate-type": "毕业证明"}, "one of"),
        ({"applicant-name": "张", "certificate-type": "在读证明", "nickname": "x"}, "unknown"),
    ):
        with pytest.raises(InvalidEHallForm, match=expected):
            build_field_values(snapshot, supplied)


def test_an_optional_field_may_be_left_out() -> None:
    snapshot = _snapshot(
        fields=(
            _field(),
            _field(key="remarks", label="备注", kind=EHallFieldKind.TEXTAREA, required=False),
        )
    )

    values = build_field_values(snapshot, {"applicant-name": "张同学"})

    assert [item.key for item in values] == ["applicant-name"]


def test_a_payload_round_trips_and_keeps_the_fixed_consequence() -> None:
    snapshot = _snapshot()
    payload = EHallCertificatePayload(
        fields=build_field_values(
            snapshot, {"applicant-name": "张同学", "certificate-type": "在读证明"}
        ),
        page_contract_fingerprint=snapshot.fingerprint(),
        required_materials=snapshot.required_materials,
    )

    restored = EHallCertificatePayload.from_payload(payload.to_payload())

    assert restored == payload
    assert payload.consequence == CERTIFICATE_CONSEQUENCE
    assert "administrative record" in payload.consequence


@pytest.mark.parametrize(
    "overrides",
    [
        {"fields": ()},
        {"page_contract_fingerprint": "short"},
        {"page_contract_fingerprint": "A" * 64},
        {"service_identity": "somewhere-else"},
        {"pipeline_id": "ehall.other"},
        {"consequence": "A friendlier-sounding summary."},
        {"schema_version": 99},
    ],
)
def test_a_payload_refuses_anything_it_was_not_meant_to_carry(
    overrides: dict[str, object]
) -> None:
    snapshot = _snapshot()
    values: dict[str, object] = {
        "fields": (
            EHallCertificateFieldValue(
                key="applicant-name",
                label="申请人姓名",
                kind=EHallFieldKind.TEXT,
                value="张同学",
            ),
        ),
        "page_contract_fingerprint": snapshot.fingerprint(),
    }
    values.update(overrides)
    with pytest.raises(InvalidEHallForm):
        EHallCertificatePayload(**values)  # type: ignore[arg-type]


def test_stored_payloads_are_read_strictly() -> None:
    snapshot = _snapshot()
    values = build_field_values(
        snapshot, {"applicant-name": "张同学", "certificate-type": "在读证明"}
    )
    stored = EHallCertificatePayload(
        fields=values, page_contract_fingerprint=snapshot.fingerprint()
    ).to_payload()

    for broken in (
        {},
        "not an object",
        {**stored, "submit_selector": "#submit"},
        {key: value for key, value in stored.items() if key != "fields"},
        {**stored, "fields": "not a list"},
        {**stored, "fields": [{"key": "x", "label": "y", "kind": "nonsense", "value": "z"}]},
        {**stored, "fields": [{"key": "x", "label": "y", "kind": "text"}]},
    ):
        with pytest.raises(InvalidEHallForm):
            EHallCertificatePayload.from_payload(broken)


def test_a_field_value_must_be_non_blank_and_bounded() -> None:
    with pytest.raises(InvalidEHallForm):
        EHallCertificateFieldValue(
            key="k", label="l", kind=EHallFieldKind.TEXT, value="   "
        )
    with pytest.raises(InvalidEHallForm):
        EHallCertificateFieldValue(
            key="k", label="l", kind=EHallFieldKind.TEXT, value="x" * 2001
        )
