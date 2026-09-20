"""Preparing an approvable certificate application (ADR-0025).

```text
pw ehall certificate prepare --case CASE --field KEY=VALUE …
        │
        ├── the case must be OPEN
        ├── live read-only inspection of the whitelisted service
        ├── require a form this pipeline can actually fill
        ├── validate every supplied key, required field and option locally
        ├── freeze the contract fingerprint and the exact values into a payload
        ▼
immutable ActionRequest("ehall.submit-certificate")
```

**Nothing is typed during preparation.** The remote form is read, never touched: a preparation
that filled fields would trigger the portal's own autosave and validation before the user had
approved anything. What the user approves is a *description* of the errand, and the executor is
what turns it into keystrokes — after an approval has been consumed.

All values come from `--field KEY=VALUE`. There is no knowledge lookup, no mail analysis and no
model anywhere on this path: the phase has no personal-fact autofill, by design.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from assistant.domain.action import ActionRequest, ActionType
from assistant.domain.case import Case, CaseId, CaseStatus
from assistant.domain.ehall import (
    EHallCertificatePayload,
    EHallCertificatePreview,
    EHallFieldDefinition,
    EHallFormSnapshot,
    EHallUnsupportedControl,
    build_field_values,
)
from assistant.domain.errors import (
    CaseNotFound,
    CaseNotOpen,
    EHallDisabled,
)
from assistant.ports.action_repository import ActionRepository
from assistant.ports.case_repository import CaseRepository
from assistant.ports.clock import Clock
from assistant.ports.ehall_certificate import EHallCertificateGateway

LOGGER = logging.getLogger("assistant.ehall")

E_HALL_CERTIFICATE_ACTION_TYPE = ActionType("ehall.submit-certificate")
"""The action type every prepared certificate application uses."""


@dataclass(frozen=True, slots=True)
class EHallCertificatePreparation:
    """The prepared action plus the preview the user is about to approve."""

    action: ActionRequest
    preview: EHallCertificatePreview


@dataclass(frozen=True, slots=True)
class EHallInspection:
    """What one read-only inspection found, for the CLI to display."""

    snapshot: EHallFormSnapshot

    @property
    def fields(self) -> tuple[EHallFieldDefinition, ...]:
        """The fillable fields, in page order."""
        return self.snapshot.fields

    @property
    def unsupported(self) -> tuple[EHallUnsupportedControl, ...]:
        """Controls this pipeline will not fill."""
        return self.snapshot.unsupported

    @property
    def required_materials(self) -> tuple[str, ...]:
        """What the service says the applicant must have ready."""
        return self.snapshot.required_materials

    @property
    def fingerprint(self) -> str:
        """The page contract's fingerprint."""
        return self.snapshot.fingerprint()


class EHallCertificateService:
    """Inspects the certificate form and prepares an exact, approvable submission."""

    def __init__(
        self,
        gateway: EHallCertificateGateway,
        cases: CaseRepository,
        actions: ActionRepository,
        clock: Clock,
        *,
        enabled: bool = True,
    ) -> None:
        self._gateway = gateway
        self._cases = cases
        self._actions = actions
        self._clock = clock
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        """Whether this host has switched the pipeline on."""
        return self._enabled

    async def inspect(self) -> EHallInspection:
        """Read the live certificate form. Strictly read-only.

        Raises:
            EHallDisabled: the host has not enabled the pipeline.
            EHallLoginRequired: no usable session exists.
            EHallServiceMismatch: the service or its markers did not match.
            EHallUnsupportedRequiredField: the form needs a control this pipeline cannot fill.
        """
        self._require_enabled()
        snapshot = await self._gateway.inspect_form()
        return EHallInspection(snapshot=snapshot)

    async def prepare(
        self,
        *,
        case_id: CaseId | str,
        field_values: dict[str, str],
    ) -> EHallCertificatePreparation:
        """Prepare one exact certificate submission for one open case.

        Raises:
            EHallDisabled: the host has not enabled the pipeline.
            CaseNotFound: no such case.
            CaseNotOpen: the case is terminal.
            InvalidEHallForm: a key is unknown, a required field is missing, or a value is not one
                of the options the page offers.
            EHallLoginRequired / EHallServiceMismatch / EHallUnsupportedRequiredField: the live
                form cannot be used.
        """
        self._require_enabled()
        case = await self._require_open_case(case_id)
        inspection = await self.inspect()
        snapshot = inspection.snapshot
        values = build_field_values(snapshot, field_values)
        payload = EHallCertificatePayload(
            fields=values,
            page_contract_fingerprint=snapshot.fingerprint(),
            required_materials=snapshot.required_materials,
        )
        action = ActionRequest.prepare(
            case_id=case.id,
            action_type=E_HALL_CERTIFICATE_ACTION_TYPE,
            payload=payload.to_payload(),
            at=self._clock.now(),
        )
        stored = await self._actions.add_action(action)
        LOGGER.info(
            "ehall certificate prepared fields=%d materials=%d",
            len(values),
            len(snapshot.required_materials),
        )
        return EHallCertificatePreparation(
            action=stored,
            preview=EHallCertificatePreview(
                service_identity=payload.service_identity,
                fields=payload.fields,
                required_materials=payload.required_materials,
                consequence=payload.consequence,
                page_contract_fingerprint=payload.page_contract_fingerprint,
                unsupported=snapshot.unsupported,
            ),
        )

    async def preview(self, action: ActionRequest) -> EHallCertificatePreview:
        """Rebuild the preview of an already-prepared action, without touching a browser."""
        payload = EHallCertificatePayload.from_payload(action.payload)
        return EHallCertificatePreview(
            service_identity=payload.service_identity,
            fields=payload.fields,
            required_materials=payload.required_materials,
            consequence=payload.consequence,
            page_contract_fingerprint=payload.page_contract_fingerprint,
        )

    # ------------------------------------------------------------------ internals

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise EHallDisabled(
                "the eHall pipeline is disabled; set [ehall] enabled = true to use it"
            )

    async def _require_open_case(self, case_id: CaseId | str) -> Case:
        resolved = (
            case_id
            if not isinstance(case_id, str)
            else await self._cases.resolve_case_id(case_id)
        )
        case = await self._cases.get_case(resolved)
        if case is None:
            raise CaseNotFound(case_id)
        if case.status is not CaseStatus.OPEN:
            raise CaseNotOpen(case.id, case.status.value)
        return case


__all__ = [
    "E_HALL_CERTIFICATE_ACTION_TYPE",
    "EHallCertificatePreparation",
    "EHallCertificateService",
    "EHallInspection",
]
