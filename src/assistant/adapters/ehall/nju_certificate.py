"""The whitelisted NJU certificate pipeline (ADR-0025).

```text
inspect_form()        open → verify service → verify markers → read the contract → close
submit_certificate()  open → verify service → verify markers → re-read the contract
                          → require fingerprint == approved
                          → fill only the approved values
                          → read every value back and compare
                          → click the one whitelisted submit control
                          → classify the result
```

The policy lives here and the browser lives behind `EHallPage`, a deliberately tiny interface, so
every rule above is testable without Chromium, and Playwright types never leave this package.

What the pipeline will not do is as important as what it does: it will not fill a field the user
did not write, it will not guess a service, it will not continue on a page whose contract changed,
it will not click anything but the one submit control, and it will not decide that an ambiguous
result was a success.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from assistant.domain.ehall import (
    CERTIFICATE_SERVICE_IDENTITY,
    CERTIFICATE_SERVICE_NAME,
    EHallCertificatePayload,
    EHallFieldDefinition,
    EHallFieldKind,
    EHallFormSnapshot,
    EHallUnsupportedControl,
)
from assistant.domain.errors import (
    EHallDisabled,
    EHallLoginRequired,
    EHallPageChanged,
    EHallServiceMismatch,
)
from assistant.ports.ehall_certificate import (
    EHallSubmissionOutcome,
    EHallSubmissionResult,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from assistant.adapters.ehall.session import EHallBrowserSession

LOGGER = logging.getLogger("assistant.ehall")

SUBMIT_CONTROL_ID = "certificate-submit"
"""The one control this pipeline may click. Its identity is part of the page contract."""

PAGE_MARKERS: tuple[str, ...] = ("证明书申请", "申请")
"""Text the service page must contain. A page without them is not the page we reviewed."""

SERVICE_CARD_SELECTOR = "[data-ehall-service]"
SERVICE_TITLE_SELECTOR = "[data-ehall-service-title]"
FORM_SELECTOR = "form[data-ehall-form]"
SUBMIT_SELECTOR = "[data-ehall-submit]"
SUCCESS_MARKER_SELECTOR = "[data-ehall-result='accepted']"
REJECTION_MARKER_SELECTOR = "[data-ehall-result='rejected']"
SESSION_LOST_SELECTOR = "[data-ehall-session='expired']"
MATERIALS_SELECTOR = "[data-ehall-material]"
FIELD_SELECTOR = "[data-ehall-field]"


@dataclass(frozen=True, slots=True)
class RawField:
    """One control as the page described it, before the pipeline decides what it means."""

    key: str | None
    label: str
    kind: str
    required: bool
    options: tuple[str, ...] = ()

    @property
    def supported(self) -> bool:
        """Whether this pipeline can fill it at all."""
        return self.kind in {item.value for item in EHallFieldKind}


@dataclass(frozen=True, slots=True)
class RawForm:
    """One observation of the page: markers, materials, controls and the submit control."""

    service_name: str | None
    markers: tuple[str, ...] = ()
    materials: tuple[str, ...] = ()
    fields: tuple[RawField, ...] = ()
    submit_control: str = ""
    session_expired: bool = False


class EHallPage(Protocol):
    """The narrow page interface the pipeline uses. Playwright satisfies it; tests fake it."""

    async def open_service(self, service_name: str) -> str:
        """Open the named service and return the page URL.

        Raises:
            EHallServiceMismatch: zero or several services carry that exact name.
        """
        ...

    async def read_service(self) -> RawForm:
        """Read the current page: its title, markers, materials and controls."""
        ...

    async def fill(self, definition: EHallFieldDefinition, value: str) -> None: ...

    async def read_back(self, definition: EHallFieldDefinition) -> str: ...

    async def click_submit(self, control: str) -> None: ...

    async def await_result(self, timeout_ms: int) -> str:
        """Return `accepted`, `rejected` or `unknown` after a submit click."""
        ...

    async def close(self) -> None: ...


PageFactory = Callable[["EHallBrowserSession"], Awaitable[EHallPage]]


class NjuCertificateGateway:
    """The certificate pipeline over a headed, manually-logged-in browser session."""

    def __init__(
        self,
        session_factory: Callable[[], EHallBrowserSession],
        page_factory: PageFactory,
        *,
        enabled: bool = True,
        timeout_seconds: int = 30,
    ) -> None:
        self._session_factory = session_factory
        self._page_factory = page_factory
        self._enabled = enabled
        self._timeout_seconds = timeout_seconds

    @property
    def enabled(self) -> bool:
        """Whether this host has switched the pipeline on."""
        return self._enabled

    async def inspect_form(self) -> EHallFormSnapshot:
        """Read the certificate form. Nothing is typed."""
        self._require_enabled()
        session = self._session_factory()
        page: EHallPage | None = None
        try:
            await session.start()
            page = await self._page_factory(session)
            return await self._snapshot(page)
        finally:
            await _close(page, session)

    async def submit_certificate(
        self,
        request: EHallCertificatePayload,
        *,
        expected_fingerprint: str,
    ) -> EHallSubmissionResult:
        """Fill exactly the approved values and click submit once.

        Every failure before the click returns `FAILED` with `submitted=False`, so the caller can
        say with confidence that nothing was submitted. A failure after the click is `UNKNOWN`.
        """
        self._require_enabled()
        session = self._session_factory()
        page: EHallPage | None = None
        submitted = False
        try:
            await session.start()
            page = await self._page_factory(session)
            live = await self._snapshot(page)
            if live.fingerprint() != expected_fingerprint:
                # The page the user reviewed is not the page in front of us any more.
                raise EHallPageChanged(expected_fingerprint)
            definitions = {item.key: item for item in live.fields}
            # Validate every approved value against the live form *before* typing anything, so a
            # form that changed in any way is refused without a single keystroke.
            for value in request.fields:
                definition = definitions.get(value.key)
                if definition is None:  # pragma: no cover - the fingerprint covers the keys
                    return EHallSubmissionResult(
                        outcome=EHallSubmissionOutcome.FAILED,
                        summary=f"the live form no longer has field {value.key!r}",
                    )
                if definition.options and value.value not in definition.options:
                    return EHallSubmissionResult(
                        outcome=EHallSubmissionOutcome.FAILED,
                        summary=(
                            f"field {value.key!r} no longer accepts the approved value; "
                            "nothing was typed"
                        ),
                    )
            for value in request.fields:
                await page.fill(definitions[value.key], value.value)
            for value in request.fields:
                definition = definitions[value.key]
                readback = (await page.read_back(definition)).strip()
                if readback != value.value:
                    # Nothing has been submitted, and nothing will be: a mismatch between what was
                    # approved and what the page holds is a hard stop.
                    return EHallSubmissionResult(
                        outcome=EHallSubmissionOutcome.FAILED,
                        summary=(
                            f"the value in field {value.key!r} did not match the approved value "
                            "after filling; nothing was submitted"
                        ),
                    )
            # From here on the click may have been dispatched even if this call raises — a
            # Playwright click waits for the page to settle, and a timeout can happen *after* the
            # mouse event. Claiming "nothing was submitted" would be a guess, so the conservative
            # reading applies: anything from this point is UNKNOWN unless the page decides.
            submitted = True
            await page.click_submit(live.submit_control)
            result = await page.await_result(self._timeout_seconds * 1000)
        except asyncio.CancelledError:
            # Cancellation is not a business outcome: whatever happened, a later reconciliation
            # decides, and the run stays RUNNING.
            raise
        except Exception as exc:
            if submitted:
                return EHallSubmissionResult(
                    outcome=EHallSubmissionOutcome.UNKNOWN,
                    summary=(
                        f"the submit click was sent but the result could not be read "
                        f"({type(exc).__name__})"
                    ),
                    submitted=True,
                )
            if isinstance(exc, (EHallPageChanged, EHallLoginRequired, EHallServiceMismatch)):
                # A contract change, an expired session and a page this pipeline does not
                # recognise are all *definite*: nothing was typed and nothing was submitted, so
                # calling them UNKNOWN would block the action for no reason. The domain message
                # is kept, because it tells the user exactly what to do next.
                return EHallSubmissionResult(
                    outcome=EHallSubmissionOutcome.FAILED,
                    summary=str(exc),
                )
            return EHallSubmissionResult(
                outcome=EHallSubmissionOutcome.FAILED,
                summary=f"the form could not be completed before submitting ({type(exc).__name__})",
            )
        finally:
            await _close(page, session)
        if result == "accepted":
            return EHallSubmissionResult(
                outcome=EHallSubmissionOutcome.SUCCEEDED,
                summary="the eHall accepted the certificate application",
                submitted=True,
            )
        if result == "rejected":
            return EHallSubmissionResult(
                outcome=EHallSubmissionOutcome.FAILED,
                summary="the eHall explicitly rejected the submission",
                submitted=True,
            )
        return EHallSubmissionResult(
            outcome=EHallSubmissionOutcome.UNKNOWN,
            summary=(
                "the submit click was sent but the eHall did not show a decisive result; "
                "check the portal before doing anything else"
            ),
            submitted=True,
        )

    # ------------------------------------------------------------------ internals

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise EHallDisabled(
                "the eHall pipeline is disabled; set [ehall] enabled = true to use it"
            )

    async def _snapshot(self, page: EHallPage) -> EHallFormSnapshot:
        """Open the whitelisted service and read its contract, refusing anything unfamiliar."""
        await page.open_service(CERTIFICATE_SERVICE_NAME)
        raw = await page.read_service()
        if raw.session_expired:
            raise EHallLoginRequired(
                "the eHall session is not logged in; run `pw ehall login`"
            )
        if raw.service_name != CERTIFICATE_SERVICE_NAME:
            raise EHallServiceMismatch(
                f"expected the {CERTIFICATE_SERVICE_NAME!r} service, "
                f"found {raw.service_name!r}"
            )
        text = " ".join(raw.markers)
        missing = [marker for marker in PAGE_MARKERS if marker not in text]
        if missing:
            raise EHallServiceMismatch(
                "the certificate page did not show the markers this pipeline requires: "
                + ", ".join(missing)
            )
        fields: list[EHallFieldDefinition] = []
        unsupported: list[EHallUnsupportedControl] = []
        for position, item in enumerate(raw.fields, start=1):
            if not item.supported:
                unsupported.append(
                    EHallUnsupportedControl(
                        label=item.label or f"control {position}",
                        description=f"unsupported control type {item.kind!r}",
                        required=item.required,
                    )
                )
                continue
            fields.append(
                EHallFieldDefinition(
                    key=item.key or f"field-{position:02d}",
                    label=item.label or f"field {position}",
                    kind=EHallFieldKind(item.kind),
                    required=item.required,
                    options=item.options,
                )
            )
        snapshot = EHallFormSnapshot(
            service_identity=CERTIFICATE_SERVICE_IDENTITY,
            fields=tuple(fields),
            required_materials=raw.materials,
            unsupported=tuple(unsupported),
            submit_control=raw.submit_control or SUBMIT_CONTROL_ID,
            page_markers=PAGE_MARKERS,
        )
        snapshot.require_supported()
        return snapshot


async def _close(page: EHallPage | None, session: EHallBrowserSession) -> None:
    """Close the page and the browser, without masking the real outcome."""
    if page is not None:
        try:
            await page.close()
        except Exception:  # pragma: no cover - a failed close must not change the result
            LOGGER.debug("eHall page close failed")
    try:
        await session.close()
    except Exception:  # pragma: no cover - a failed close must not change the result
        LOGGER.debug("eHall session close failed")


__all__ = [
    "FIELD_SELECTOR",
    "FORM_SELECTOR",
    "MATERIALS_SELECTOR",
    "PAGE_MARKERS",
    "REJECTION_MARKER_SELECTOR",
    "SERVICE_CARD_SELECTOR",
    "SERVICE_TITLE_SELECTOR",
    "SESSION_LOST_SELECTOR",
    "SUBMIT_CONTROL_ID",
    "SUBMIT_SELECTOR",
    "SUCCESS_MARKER_SELECTOR",
    "EHallPage",
    "NjuCertificateGateway",
    "PageFactory",
    "RawField",
    "RawForm",
]
