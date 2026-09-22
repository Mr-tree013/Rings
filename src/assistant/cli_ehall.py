"""`pw ehall` — the whitelisted certificate pipeline (ADR-0025).

```bash
pw ehall login                     open a headed browser; the user completes SSO by hand
pw ehall status                    local, read-only capability report
pw ehall certificate inspect       read the live form (network + browser, read-only)
pw ehall certificate prepare …     freeze a submission into an approvable action
pw ehall certificate show ACTION   the critical-field preview
```

There is deliberately no `pw ehall submit`, `click`, `open`, `fill` or `form`: the only way
anything is submitted is `pw action execute`, after a human approval bound to the exact action
payload. This module never types a credential and never accepts a URL, a service name or a
selector.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

import typer
from rich.table import Table

from assistant import bootstrap
from assistant.adapters.ehall.session import (
    EHALL_HOME_URL,
    EHallBrowserSession,
    session_state,
)
from assistant.application.action_service import ActionService
from assistant.application.ehall_certificate import (
    EHallCertificatePreparation,
    EHallInspection,
)
from assistant.cli_support import console, fail, short_id
from assistant.domain.action import ActionRequest
from assistant.domain.config import AssistantConfig
from assistant.domain.ehall import (
    CERTIFICATE_SERVICE_NAME,
    EHallCertificatePreview,
)
from assistant.domain.errors import (
    ActionRequestNotFound,
    AmbiguousId,
    CaseNotFound,
    CaseNotOpen,
    DomainError,
    EHallActionMismatch,
    EHallBrowserUnavailable,
    EHallDisabled,
    EHallLoginRequired,
    EHallPageChanged,
    EHallServiceMismatch,
    EHallUnexpectedOrigin,
    EHallUnsupportedRequiredField,
    InvalidAssistantConfig,
    InvalidEHallForm,
)
from assistant.store.errors import StoreError

ehall_app = typer.Typer(
    help="eHall: the whitelisted certificate pipeline. Submission requires an approval.",
    no_args_is_help=True,
)
certificate_app = typer.Typer(
    help="The certificate application service only.", no_args_is_help=True
)
ehall_app.add_typer(certificate_app, name="certificate")

_EXPECTED_FAILURES = (
    ActionRequestNotFound,
    EHallActionMismatch,
    AmbiguousId,
    CaseNotFound,
    CaseNotOpen,
    EHallActionMismatch,
    EHallBrowserUnavailable,
    EHallDisabled,
    EHallLoginRequired,
    EHallPageChanged,
    EHallServiceMismatch,
    EHallUnexpectedOrigin,
    EHallUnsupportedRequiredField,
    InvalidEHallForm,
)


def _run[T](action: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Run one async action over fresh services, mapping project errors to CLI failures."""
    try:
        return asyncio.run(action())
    except _EXPECTED_FAILURES as exc:
        fail(str(exc))
    except DomainError as exc:
        fail(str(exc))
    except StoreError as exc:
        fail(f"store failure: {exc}")


def _load_config_or_fail() -> AssistantConfig:
    """Load the host configuration, or fail with a readable message."""
    try:
        return asyncio.run(bootstrap.config_loader().load())
    except InvalidAssistantConfig as exc:
        fail(f"invalid configuration: {exc}", code=2)


def _require_enabled(config: AssistantConfig) -> None:
    if not config.ehall.enabled:
        fail(
            "the eHall pipeline is disabled; set [ehall] enabled = true in the host config "
            "to use it"
        )


@ehall_app.command("login")
def ehall_login() -> None:
    """Open a headed browser so you can log in to eHall by hand.

    This project never asks for your university password, never types one, and never stores one.
    Complete SSO and MFA in the window that opens; the session is kept in a private profile under
    the runtime data directory.
    """
    config = _load_config_or_fail()
    _require_enabled(config)
    console.print(f"opening {EHALL_HOME_URL} — complete the login in the browser window")
    console.print("the profile is kept outside the repository, with owner-only permissions")
    _run(lambda: _login(config))
    console.print("[green]session saved[/green]")


async def _login(config: AssistantConfig) -> None:
    session = EHallBrowserSession(timeout_seconds=config.ehall.timeout_seconds)
    async with session:
        page = await session.open_ehall_home()
        # The user finishes the login in the window; this process only waits and watches the
        # top-level origin allow-list. It never fills a field.
        await _wait_for_login(page)


async def _wait_for_login(page: object) -> None:
    """Wait until the user closes the window or navigates away from the login flow."""
    try:
        await page.wait_for_timeout(0)  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - defensive
        return
    while True:
        try:
            if page.is_closed():  # type: ignore[attr-defined]
                return
        except Exception:  # pragma: no cover - the page went away
            return
        await asyncio.sleep(1)


@ehall_app.command("status")
def ehall_status() -> None:
    """Show the local eHall capability state. This never contacts a server."""
    config = _load_config_or_fail()
    state = session_state()
    table = Table(title="ehall status", show_header=False, title_justify="left")
    table.add_row("Enabled", "yes" if config.ehall.enabled else "no")
    table.add_row("Timeout", f"{config.ehall.timeout_seconds}s")
    table.add_row("Playwright package", "installed" if state.playwright_installed else "missing")
    # The detail names the exact build this Playwright needs, and never reports a stale cache as
    # usable: that is what "available" has to mean for a status command to be worth reading.
    table.add_row("Chromium runtime", state.chromium_detail)
    table.add_row("Profile directory", str(state.profile_dir))
    table.add_row("Profile present", "yes" if state.profile_exists else "no")
    table.add_row("Production pipeline", f"certificate application ({CERTIFICATE_SERVICE_NAME})")
    table.add_row(
        "Executor registered",
        "yes" if config.ehall.enabled else "no (pipeline disabled)",
    )
    console.print(table)
    console.print(
        "This report says nothing about whether the saved session is still logged in: "
        "only a live inspection can tell you that."
    )


@certificate_app.command("inspect")
def certificate_inspect() -> None:
    """Read the live certificate form. This opens a browser and submits nothing."""
    config = _load_config_or_fail()
    inspection = _run(lambda: _inspect(config))
    _print_inspection(inspection)


async def _inspect(config: AssistantConfig) -> EHallInspection:
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    service = bootstrap.ehall_certificate_service(config, clock, database)
    return await service.inspect()


def _print_inspection(inspection: EHallInspection) -> None:
    snapshot = inspection.snapshot
    console.print(f"[bold]Service[/bold] {snapshot.service_identity}")
    console.print("")
    console.print("[bold]Required materials / instructions[/bold]")
    if snapshot.required_materials:
        for material in snapshot.required_materials:
            console.print(f"- {material}")
    else:
        console.print("- (the service listed none)")
    console.print("")
    table = Table(title="fields")
    for column in ("Key", "Label", "Type", "Required", "Allowed options"):
        table.add_column(column)
    for definition in inspection.fields:
        table.add_row(
            definition.key,
            definition.label,
            definition.kind.value,
            "yes" if definition.required else "no",
            ", ".join(definition.options) or "-",
        )
    console.print(table)
    if inspection.unsupported:
        unsupported = Table(title="unsupported controls")
        for column in ("Label", "Detail", "Required"):
            unsupported.add_column(column)
        for control in inspection.unsupported:
            unsupported.add_row(
                control.label,
                control.description,
                "yes" if control.required else "no",
            )
        console.print(unsupported)
    else:
        console.print("unsupported required controls: none")
    console.print(f"page contract fingerprint: {inspection.fingerprint}")
    console.print("")
    console.print("[bold]Nothing was typed and nothing was submitted.[/bold]")


@certificate_app.command("prepare")
def certificate_prepare(
    case: Annotated[str, typer.Option("--case", help="The open case this errand belongs to.")],
    field: Annotated[
        list[str] | None,
        typer.Option(
            "--field",
            help="A form value as KEY=VALUE. Repeat for every field you want to fill.",
        ),
    ] = None,
) -> None:
    """Freeze one certificate submission into an exact, approvable action.

    You supply every value; nothing is autofilled from your knowledge base, your mail or a model.
    """
    config = _load_config_or_fail()
    values = _parse_fields(field or [])
    preparation = _run(lambda: _prepare(config, case, values))
    _print_preparation(preparation)


def _parse_fields(values: list[str]) -> dict[str, str]:
    """Parse repeated `--field KEY=VALUE` options, refusing anything malformed."""
    parsed: dict[str, str] = {}
    for item in values:
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            fail(f"--field expects KEY=VALUE, not {item!r}")
        if key.strip() in parsed:
            fail(f"--field {key.strip()!r} was given twice")
        parsed[key.strip()] = value
    return parsed


async def _prepare(
    config: AssistantConfig, case: str, values: dict[str, str]
) -> EHallCertificatePreparation:
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    service = bootstrap.ehall_certificate_service(config, clock, database)
    return await service.prepare(case_id=case, field_values=values)


def _print_preparation(preparation: EHallCertificatePreparation) -> None:
    preview = preparation.preview
    console.print("[green]Prepared exact eHall certificate action.[/green]")
    console.print(f"Action:       {preparation.action.id}")
    console.print(f"Case:         {preparation.action.case_id}")
    console.print(f"Service:      {preview.service_identity}")
    console.print(f"Fingerprint:  {preparation.action.fingerprint}")
    console.print(f"Page contract: {preview.page_contract_fingerprint}")
    console.print("")
    _print_preview_fields(preview)
    console.print("")
    console.print("[bold]Nothing was submitted.[/bold]")
    console.print("")
    console.print("Next:")
    console.print(f"  pw action challenge {short_id(preparation.action.id)}")


def _print_preview_fields(preview: EHallCertificatePreview) -> None:
    table = Table(title="critical fields")
    for column in ("Label", "Value", "Type"):
        table.add_column(column)
    for value in preview.fields:
        table.add_row(value.label, value.value, value.kind.value)
    console.print(table)
    console.print("[bold]Required materials[/bold]")
    materials = preview.required_materials
    if materials:
        for material in materials:
            console.print(f"- {material}")
    else:
        console.print("- (the service listed none)")
    console.print("")
    console.print("[bold]Consequence[/bold]")
    console.print(preview.consequence)


@certificate_app.command("show")
def certificate_show(
    reference: Annotated[str, typer.Argument(help="Action id or unique prefix.")]
) -> None:
    """Show the critical-field preview of an already prepared action.

    This never opens a browser and never contacts the university.
    """
    config = _load_config_or_fail()
    action, preview, approval, execution = _run(lambda: _show(config, reference))
    table = Table(
        title=f"ehall certificate {short_id(action.id)}",
        show_header=False,
        title_justify="left",
    )
    table.add_row("Service", preview.service_identity)
    table.add_row("Case", str(action.case_id))
    table.add_row("Action ID", str(action.id))
    table.add_row("Action status", action.status.value)
    table.add_row("Fingerprint", action.fingerprint)
    table.add_row("Page contract", preview.page_contract_fingerprint)
    table.add_row("Approval", approval)
    table.add_row("Execution", execution)
    console.print(table)
    _print_preview_fields(preview)
    console.print(
        "[bold]Nothing is submitted by this command.[/bold] Use `pw action challenge`, "
        "`pw action approve` and `pw action execute` to continue."
    )


async def _show(
    config: AssistantConfig, reference: str
) -> tuple[ActionRequest, EHallCertificatePreview, str, str]:
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    actions = ActionService(bootstrap.action_repository(database), clock)
    action = await actions.require_action(reference)
    if action.action_type.value != "ehall.submit-certificate":
        raise EHallActionMismatch(
            action.id, f"its action type is {action.action_type.value}"
        )
    service = bootstrap.ehall_certificate_service(config, clock, database)
    preview = await service.preview(action)
    overview = await actions.overview(action.id)
    return (
        action,
        preview,
        overview.approval_state.value,
        "none" if overview.execution is None else overview.execution.status.value,
    )


def register(app: typer.Typer) -> None:
    """Register the `pw ehall` group on the root app."""
    app.add_typer(ehall_app, name="ehall")


__all__ = ["certificate_app", "ehall_app", "register"]
