"""EHallCertificateGateway port: exactly two operations, both about one service (ADR-0025).

There is deliberately no `BrowserPort` here, and no `goto`, `click`, `fill`, `evaluate` or
`submit_any_form`. A caller cannot name a URL, a selector or a page, so the application layer and
anything driving a model have no vocabulary in which to express "open a page and do something on
it". The two operations that exist are the two the certificate errand needs:

```text
inspect_form()                     read the service's form, change nothing
submit_certificate(request)        re-verify the contract, fill the approved values, submit once
```

`submit_certificate` takes the *approved* payload and the fingerprint the approval was bound to.
Anything else — a re-inspection that differs, a value the user did not write, a second submit —
is the implementation's job to refuse.

Nothing here exposes Playwright types: the adapter owns them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from assistant.domain.ehall import EHallCertificatePayload, EHallFormSnapshot


class EHallSubmissionOutcome(StrEnum):
    """How one submission attempt ended.

    `SUCCEEDED` requires an explicit success state on the page. `FAILED` means the attempt ended
    before, or explicitly at, the university's side of the boundary without being accepted.
    `UNKNOWN` means the submit click happened and the result could not be determined — the case
    that must never be retried automatically.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class EHallSubmissionResult:
    """The outcome plus a bounded, non-content summary for the execution record."""

    outcome: EHallSubmissionOutcome
    summary: str
    submitted: bool = False
    """Whether the whitelisted submit control was actually clicked in this attempt."""

    def __post_init__(self) -> None:
        if not self.summary.strip():
            raise ValueError("a submission result needs a summary")
        if self.outcome is EHallSubmissionOutcome.SUCCEEDED and not self.submitted:
            raise ValueError("a SUCCEEDED submission must have clicked submit")


class EHallCertificateGateway(Protocol):
    """The whitelisted certificate pipeline, as seen by the rest of the project."""

    async def inspect_form(self) -> EHallFormSnapshot:
        """Read the certificate form without changing it.

        Raises:
            EHallDisabled: the host has not enabled the pipeline.
            EHallLoginRequired: no usable session exists.
            EHallBrowserUnavailable: Playwright or its Chromium runtime is not usable.
            EHallServiceMismatch: the service could not be identified exactly.
            EHallUnexpectedOrigin: navigation left the allowed origins.
            EHallUnsupportedRequiredField: the form requires an unsupported control.
        """
        ...

    async def submit_certificate(
        self,
        request: EHallCertificatePayload,
        *,
        expected_fingerprint: str,
    ) -> EHallSubmissionResult:
        """Fill the approved values and click the whitelisted submit control once.

        The implementation re-inspects the live page and refuses when its contract no longer
        matches `expected_fingerprint`.

        Raises:
            EHallPageChanged: the live page no longer matches the approved contract.
            EHallLoginRequired: the session expired before anything was typed.
            EHallServiceMismatch: the service or page marker did not match.
            EHallUnexpectedOrigin: navigation left the allowed origins.
        """
        ...


__all__ = [
    "EHallCertificateGateway",
    "EHallSubmissionOutcome",
    "EHallSubmissionResult",
]
