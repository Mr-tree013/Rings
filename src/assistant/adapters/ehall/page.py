"""The Playwright implementation of the pipeline's narrow page interface (ADR-0025).

This is the only module that names a selector or a locator, and it is imported by nothing except
the eHall adapter package. Everything above it sees `RawField`, `RawForm` and a handful of
verbs — never a `Page`, never a CSS selector, never `evaluate`.

The page is expected to carry `data-ehall-*` attributes. That is a convention this project asks
the adapter to rely on, not a claim about the university's markup: when the real portal does not
provide them, this file is what has to change, and the page contract makes sure such a change is
noticed before anything is submitted.
"""

from __future__ import annotations

import asyncio
from typing import Any

from assistant.adapters.ehall.nju_certificate import (
    FIELD_SELECTOR,
    MATERIALS_SELECTOR,
    REJECTION_MARKER_SELECTOR,
    SERVICE_CARD_SELECTOR,
    SERVICE_TITLE_SELECTOR,
    SESSION_LOST_SELECTOR,
    SUBMIT_SELECTOR,
    SUCCESS_MARKER_SELECTOR,
    RawField,
    RawForm,
)
from assistant.domain.ehall import EHallFieldDefinition
from assistant.domain.errors import EHallServiceMismatch

RESULT_POLL_INTERVAL_MS = 250


class PlaywrightEHallPage:
    """Drives one Playwright page for the certificate pipeline."""

    def __init__(self, page: Any, *, service_card_selector: str = SERVICE_CARD_SELECTOR) -> None:
        self._page = page
        self._service_card_selector = service_card_selector

    async def open_service(self, service_name: str) -> str:
        """Click the one service card whose exact title matches, or refuse.

        The name is a constant in the pipeline. It is not a parameter, so neither the CLI nor a
        model can point this at a different service.
        """
        cards = self._page.locator(self._service_card_selector)
        count = await cards.count()
        matches = []
        for index in range(count):
            card = cards.nth(index)
            title = (await card.locator(SERVICE_TITLE_SELECTOR).inner_text()).strip()
            if title == service_name:
                matches.append(card)
        if len(matches) != 1:
            raise EHallServiceMismatch(
                f"expected exactly one service named {service_name!r}, found {len(matches)}"
            )
        await matches[0].click()
        return str(self._page.url)

    async def read_service(self) -> RawForm:
        """Read the current page: markers, materials, controls and the submit control."""
        body = await self._page.locator("body").inner_text()
        service_name = None
        title = self._page.locator(SERVICE_TITLE_SELECTOR)
        if await title.count() == 1:
            service_name = (await title.first.inner_text()).strip()
        materials = [
            " ".join((await item.inner_text()).split())
            for item in await _all(self._page.locator(MATERIALS_SELECTOR))
        ]
        fields: list[RawField] = []
        for element in await _all(self._page.locator(FIELD_SELECTOR)):
            fields.append(await _read_field(element))
        submit = self._page.locator(SUBMIT_SELECTOR)
        submit_control = ""
        if await submit.count() == 1:
            submit_control = str(
                await submit.first.get_attribute("data-ehall-submit") or ""
            )
        expired = await self._page.locator(SESSION_LOST_SELECTOR).count() > 0
        return RawForm(
            service_name=service_name,
            markers=(body,),
            materials=tuple(item for item in materials if item),
            fields=tuple(fields),
            submit_control=submit_control,
            session_expired=expired,
        )

    async def fill(self, definition: EHallFieldDefinition, value: str) -> None:
        """Fill one control with exactly the value it was given."""
        locator = self._field(definition)
        if definition.kind.value in ("text", "textarea"):
            await locator.fill(value)
        elif definition.kind.value == "select":
            await locator.select_option(label=value)
        else:
            await locator.locator(f"[value='{value}']").check()

    async def read_back(self, definition: EHallFieldDefinition) -> str:
        """Read one control's current value from the DOM."""
        locator = self._field(definition)
        if definition.kind.value == "select":
            return str(
                await locator.evaluate("node => node.selectedOptions[0]?.label ?? ''")
            ).strip()
        if definition.kind.value == "radio":
            checked = locator.locator("input:checked")
            if await checked.count() == 0:
                return ""
            return str(await checked.first.get_attribute("value") or "").strip()
        return str(await locator.input_value()).strip()

    async def click_submit(self, control: str) -> None:
        """Click the one whitelisted submit control."""
        locator = self._page.locator(f"{SUBMIT_SELECTOR}[data-ehall-submit='{control}']")
        if await locator.count() != 1:
            raise EHallServiceMismatch(
                f"expected exactly one submit control named {control!r}"
            )
        await locator.first.click()

    async def await_result(self, timeout_ms: int) -> str:
        """Wait for a decisive result, or report that there is none."""
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            if await self._page.locator(SUCCESS_MARKER_SELECTOR).count() > 0:
                return "accepted"
            if await self._page.locator(REJECTION_MARKER_SELECTOR).count() > 0:
                return "rejected"
            await asyncio.sleep(RESULT_POLL_INTERVAL_MS / 1000)
        return "unknown"

    async def close(self) -> None:
        """Close the page."""
        await self._page.close()

    def _field(self, definition: EHallFieldDefinition) -> Any:
        return self._page.locator(
            f"{FIELD_SELECTOR}[data-ehall-key='{definition.key}']"
        )


async def _all(locator: Any) -> list[Any]:
    return [locator.nth(index) for index in range(await locator.count())]


async def _read_field(element: Any) -> RawField:
    """Read one control's shape: kind, label, required flag and options."""
    kind = str(await element.get_attribute("data-ehall-kind") or "").strip()
    label = str(await element.get_attribute("data-ehall-label") or "").strip()
    required = str(await element.get_attribute("data-ehall-required") or "") == "true"
    options: tuple[str, ...] = ()
    if kind == "select":
        option_elements = await _all(element.locator("option"))
        values = [str(await item.get_attribute("value") or "").strip() for item in option_elements]
        labels = [" ".join((await item.inner_text()).split()) for item in option_elements]
        options = tuple(label for label in labels if label) or tuple(
            value for value in values if value
        )
    elif kind == "radio":
        radio_elements = await _all(element.locator("input[type='radio']"))
        radio_values: list[str] = []
        for item in radio_elements:
            candidate = str(await item.get_attribute("value") or "").strip()
            if candidate:
                radio_values.append(candidate)
        options = tuple(radio_values)
    return RawField(
        key=str(await element.get_attribute("data-ehall-key") or "").strip() or None,
        label=label,
        kind=kind,
        required=required,
        options=options,
    )


__all__ = ["RESULT_POLL_INTERVAL_MS", "PlaywrightEHallPage"]
