"""The two production dry-run validators and the registry that owns them (ADR-0028).

A validator is a pure function of one stored payload. These tests pin what it accepts, what it
refuses, and — just as important — what it never touches: no credential, no configuration, no
client, no browser, no clock.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from assistant.application.playbook_replay import (
    EHALL_CERTIFICATE_REPLAY_CONTRACT_VERSION,
    MAIL_SEND_REPLAY_CONTRACT_VERSION,
    EHallCertificateReplayValidator,
    MailSendReplayValidator,
    PlaybookReplayRegistry,
)
from assistant.domain.action import ActionRequest, ActionType
from assistant.domain.errors import PlaybookReplayUnsupported
from assistant.domain.playbook import (
    ISSUE_ACTION_TYPE_MISMATCH,
    ISSUE_PAYLOAD_INVALID,
    ISSUE_SCHEMA_VERSION_UNSUPPORTED,
)
from tests.support.playbooks import (
    EHALL_ACTION_TYPE,
    FUTURE_ACTION_TYPE,
    MAIL_SEND_ACTION_TYPE,
    ehall_payload,
    mail_send_payload,
)

NOW = datetime(2026, 9, 24, 9, 0, tzinfo=UTC)


def _action(action_type: ActionType, payload: object) -> ActionRequest:
    """A stored-shape action: the validators only ever see one of these."""
    return ActionRequest.prepare(
        case_id=uuid4(), action_type=action_type, payload=payload, at=NOW
    )


# ------------------------------------------------------------------------ registry


def test_the_production_registry_contains_exactly_the_two_capabilities() -> None:
    registry = PlaybookReplayRegistry.default()

    assert registry.action_types == (EHALL_ACTION_TYPE, MAIL_SEND_ACTION_TYPE)
    assert registry.supports(MAIL_SEND_ACTION_TYPE) is True
    assert registry.supports(EHALL_ACTION_TYPE) is True
    assert registry.supports(FUTURE_ACTION_TYPE) is False


def test_an_unknown_action_type_has_no_validator() -> None:
    registry = PlaybookReplayRegistry.default()

    with pytest.raises(PlaybookReplayUnsupported):
        registry.validator_for(FUTURE_ACTION_TYPE)


def test_an_empty_registry_validates_nothing() -> None:
    registry = PlaybookReplayRegistry()

    assert registry.action_types == ()
    assert registry.supports(MAIL_SEND_ACTION_TYPE) is False


def test_registering_two_validators_for_one_type_is_refused() -> None:
    with pytest.raises(ValueError, match="two replay validators"):
        PlaybookReplayRegistry([MailSendReplayValidator(), MailSendReplayValidator()])


def test_a_validator_must_declare_a_positive_contract_version() -> None:
    validator = MailSendReplayValidator()
    validator.contract_version = 0  # type: ignore[misc]

    with pytest.raises(ValueError, match="non-positive contract version"):
        PlaybookReplayRegistry([validator])


def test_the_declared_contract_versions_are_the_reviewed_ones() -> None:
    assert MAIL_SEND_REPLAY_CONTRACT_VERSION == 1
    assert EHALL_CERTIFICATE_REPLAY_CONTRACT_VERSION == 1
    assert MailSendReplayValidator().contract_version == 1
    assert EHallCertificateReplayValidator().contract_version == 1


# ------------------------------------------------------------------------- mail.send


def test_a_valid_mail_send_payload_passes() -> None:
    result = MailSendReplayValidator().validate(
        _action(MAIL_SEND_ACTION_TYPE, mail_send_payload())
    )

    assert result.passed is True
    assert result.issue_codes == ()


@pytest.mark.parametrize(
    "payload",
    [
        {"to": "ada@example.edu"},  # a shape from some other, imagined action
        {},
        {"schema_version": 1},
    ],
)
def test_an_unrecognisable_mail_payload_fails_with_a_bounded_code(payload: object) -> None:
    result = MailSendReplayValidator().validate(_action(MAIL_SEND_ACTION_TYPE, payload))

    assert result.passed is False
    assert result.issue_codes == (ISSUE_PAYLOAD_INVALID,)


def test_a_missing_recipient_is_a_payload_problem_not_a_crash() -> None:
    payload = mail_send_payload()
    payload["to_addresses"] = []

    result = MailSendReplayValidator().validate(_action(MAIL_SEND_ACTION_TYPE, payload))

    assert result.issue_codes == (ISSUE_PAYLOAD_INVALID,)


def test_a_future_schema_version_is_reported_as_such() -> None:
    payload = mail_send_payload()
    payload["schema_version"] = 99

    result = MailSendReplayValidator().validate(_action(MAIL_SEND_ACTION_TYPE, payload))

    assert result.issue_codes == (ISSUE_SCHEMA_VERSION_UNSUPPORTED,)


def test_a_payload_from_the_other_capability_is_an_action_type_mismatch() -> None:
    validator = MailSendReplayValidator()

    result = validator.validate(_action(EHALL_ACTION_TYPE, ehall_payload()))

    assert result.issue_codes == (ISSUE_ACTION_TYPE_MISMATCH,)


def test_a_corrupted_message_id_is_reported_as_an_invalid_payload() -> None:
    payload = mail_send_payload()
    payload["rfc_message_id"] = "not-a-message-id"

    result = MailSendReplayValidator().validate(_action(MAIL_SEND_ACTION_TYPE, payload))

    assert result.issue_codes == (ISSUE_PAYLOAD_INVALID,)


# --------------------------------------------------------- ehall.submit-certificate


def test_a_valid_certificate_payload_passes() -> None:
    result = EHallCertificateReplayValidator().validate(
        _action(EHALL_ACTION_TYPE, ehall_payload())
    )

    assert result.passed is True


def test_a_certificate_payload_with_a_bad_contract_fingerprint_fails() -> None:
    payload = ehall_payload()
    payload["page_contract_fingerprint"] = "nope"

    result = EHallCertificateReplayValidator().validate(
        _action(EHALL_ACTION_TYPE, payload)
    )

    assert result.issue_codes == (ISSUE_PAYLOAD_INVALID,)


def test_a_certificate_payload_for_another_service_fails() -> None:
    payload = ehall_payload()
    payload["service_identity"] = "some-other-service"

    result = EHallCertificateReplayValidator().validate(
        _action(EHALL_ACTION_TYPE, payload)
    )

    assert result.issue_codes == (ISSUE_PAYLOAD_INVALID,)


def test_an_edited_consequence_text_fails() -> None:
    """The consequence is fixed local text; a payload that changed it is not this pipeline's."""
    payload = ehall_payload()
    payload["consequence"] = "Nothing much happens."

    result = EHallCertificateReplayValidator().validate(
        _action(EHALL_ACTION_TYPE, payload)
    )

    assert result.issue_codes == (ISSUE_PAYLOAD_INVALID,)


def test_a_certificate_payload_with_a_future_schema_version_is_reported() -> None:
    payload = ehall_payload()
    payload["schema_version"] = 7

    result = EHallCertificateReplayValidator().validate(
        _action(EHALL_ACTION_TYPE, payload)
    )

    assert result.issue_codes == (ISSUE_SCHEMA_VERSION_UNSUPPORTED,)


def test_a_certificate_validator_refuses_the_other_capability() -> None:
    result = EHallCertificateReplayValidator().validate(
        _action(MAIL_SEND_ACTION_TYPE, mail_send_payload())
    )

    assert result.issue_codes == (ISSUE_ACTION_TYPE_MISMATCH,)


# ------------------------------------------------------------------------ purity


def test_a_validator_never_needs_a_credential_a_client_or_a_clock() -> None:
    """§36: the dry run passes offline, so it can also pass with no credential configured."""
    mail = MailSendReplayValidator()
    ehall = EHallCertificateReplayValidator()

    assert mail.validate(_action(MAIL_SEND_ACTION_TYPE, mail_send_payload())).passed
    assert ehall.validate(_action(EHALL_ACTION_TYPE, ehall_payload())).passed
    for validator in (mail, ehall):
        attributes = set(dir(validator))
        assert not {name for name in attributes if "password" in name.lower()}
        assert not {name for name in attributes if "session" in name.lower()}
        assert not {name for name in attributes if "browser" in name.lower()}
