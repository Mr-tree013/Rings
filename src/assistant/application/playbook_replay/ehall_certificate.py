"""Dry-run validation for `ehall.submit-certificate` (ADR-0028).

The certificate payload is the most context-sensitive document this project produces: its fields,
its options and its page-contract fingerprint were read from a live page the university can change
at any moment. So the validator is careful about what it claims. It re-runs the typed parser, which
checks the pipeline identity, the schema and page-contract versions, the fingerprint shape, the
service identity, the field structure and the fixed consequence text.

A pass therefore means: *this approved document is still a well-formed certificate action for the
pipeline this project implements.* It does **not** mean the NJU page still matches the recorded
contract, and it cannot mean that, because the validator never opens a browser. Anything that
would check the live page belongs to the eHall executor, after an approval, not to a dry run.
"""

from __future__ import annotations

from assistant.domain.action import ActionRequest, ActionType
from assistant.domain.ehall import CERTIFICATE_SCHEMA_VERSION, EHallCertificatePayload
from assistant.domain.errors import InvalidEHallForm
from assistant.domain.playbook import (
    ISSUE_ACTION_TYPE_MISMATCH,
    ISSUE_PAYLOAD_INVALID,
    ISSUE_SCHEMA_VERSION_UNSUPPORTED,
    ReplayValidationResult,
)

E_HALL_CERTIFICATE_ACTION_TYPE = ActionType("ehall.submit-certificate")
"""The one eHall capability, for the one whitelisted service."""

EHALL_CERTIFICATE_REPLAY_CONTRACT_VERSION = 1
"""Bump when `EHallCertificatePayload.from_payload` starts accepting or rejecting something new."""


class EHallCertificateReplayValidator:
    """Re-parses an approved certificate payload without opening a browser."""

    action_type = E_HALL_CERTIFICATE_ACTION_TYPE
    contract_version = EHALL_CERTIFICATE_REPLAY_CONTRACT_VERSION

    def validate(self, action: ActionRequest) -> ReplayValidationResult:
        """Say whether the current parser still accepts this exact payload."""
        if action.action_type != E_HALL_CERTIFICATE_ACTION_TYPE:
            return ReplayValidationResult.fail(ISSUE_ACTION_TYPE_MISMATCH)
        payload = action.payload
        if not isinstance(payload, dict):
            return ReplayValidationResult.fail(ISSUE_PAYLOAD_INVALID)
        version = payload.get("schema_version")
        if (
            isinstance(version, int)
            and not isinstance(version, bool)
            and version != CERTIFICATE_SCHEMA_VERSION
        ):
            return ReplayValidationResult.fail(ISSUE_SCHEMA_VERSION_UNSUPPORTED)
        try:
            EHallCertificatePayload.from_payload(payload)
        except (InvalidEHallForm, ValueError, TypeError):
            return ReplayValidationResult.fail(ISSUE_PAYLOAD_INVALID)
        return ReplayValidationResult.pass_()


__all__ = [
    "EHALL_CERTIFICATE_REPLAY_CONTRACT_VERSION",
    "E_HALL_CERTIFICATE_ACTION_TYPE",
    "EHallCertificateReplayValidator",
]
