"""The `ehall.submit-certificate` executor (ADR-0025).

One action type, one gateway, one outcome. This class is deliberately thin: it parses the approved
payload, asks the gateway to re-verify and submit, and maps the result onto the Phase 6A
`ExecutionOutcome`. All the browser safety lives in the gateway, and all the approval safety lives
in the execution service that called us.

`supports` is the offline preflight: the host must have the pipeline enabled and a readable
payload. It never starts a browser — that happens only after an approval has been consumed.
"""

from __future__ import annotations

from assistant.domain.action import ActionRequest, ActionType
from assistant.domain.ehall import EHallCertificatePayload
from assistant.domain.errors import InvalidEHallForm
from assistant.domain.execution import ExecutionOutcome
from assistant.ports.ehall_certificate import (
    EHallCertificateGateway,
    EHallSubmissionOutcome,
)

E_HALL_CERTIFICATE_ACTION_TYPE = ActionType("ehall.submit-certificate")
"""The one eHall capability that exists. There is no other eHall action type anywhere."""


class EHallCertificateExecutor:
    """Submits one approved certificate application."""

    action_type = E_HALL_CERTIFICATE_ACTION_TYPE

    def __init__(self, gateway: EHallCertificateGateway) -> None:
        self._gateway = gateway

    def supports(self, action: ActionRequest) -> bool:
        """Whether this host could submit this action right now. Pure and offline."""
        if action.action_type != E_HALL_CERTIFICATE_ACTION_TYPE:
            return False
        if getattr(self._gateway, "enabled", False) is not True:
            return False
        try:
            EHallCertificatePayload.from_payload(action.payload)
        except InvalidEHallForm:
            return False
        return True

    async def execute(self, action: ActionRequest) -> ExecutionOutcome:
        """Re-verify the page, fill the approved values, submit once."""
        payload = EHallCertificatePayload.from_payload(action.payload)
        result = await self._gateway.submit_certificate(
            payload, expected_fingerprint=payload.page_contract_fingerprint
        )
        if result.outcome is EHallSubmissionOutcome.SUCCEEDED:
            return ExecutionOutcome.succeeded()
        if result.outcome is EHallSubmissionOutcome.FAILED:
            return ExecutionOutcome.failed(result.summary)
        return ExecutionOutcome.unknown(result.summary)


__all__ = ["E_HALL_CERTIFICATE_ACTION_TYPE", "EHallCertificateExecutor"]
