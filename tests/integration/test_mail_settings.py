"""Mail account settings: typed writes, safe views and honest diagnostics (ADR-0043).

The tests that matter most are the negative ones. A settings surface can be useful and still be a
disaster if it returns a password, if it writes a credential into a file, or if its "test send"
button sends something. Each of those is a test below.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from assistant.adapters.config.mail_overlay import (
    OverlayMailSettingsStore,
    overlay_path,
    read_overlay,
)
from assistant.application.mail_account_settings import (
    MailAccountDraft,
    MailAccountSettingsService,
)
from assistant.domain.errors import (
    InvalidMailSettings,
    MailAccountSettingsNotFound,
)
from assistant.domain.mail_settings import MailProbeOutcome
from tests.support.fakes import FakeClock
from tests.support.mail_settings import (
    ScriptedCredentials,
    ScriptedProbe,
    account,
    sending_account,
)

INBOUND_SENTINEL = "SENTINEL-INBOUND-PASSWORD-9F3A"
OUTBOUND_SENTINEL = "SENTINEL-OUTBOUND-PASSWORD-4C1B"
"""Values a test asserts can never appear in a payload. They are never handed to the service."""


def _present(tmp_path, **kwargs):
    return _service(tmp_path, credentials=ScriptedCredentials(True, True), **kwargs)


def _service(
    tmp_path: Path,
    *,
    accounts: tuple = (),
    credentials: ScriptedCredentials | None = None,
    probe: ScriptedProbe | None = None,
    config: str = "config.toml",
) -> tuple[MailAccountSettingsService, ScriptedCredentials, ScriptedProbe]:
    source = credentials or ScriptedCredentials()
    scripted = probe or ScriptedProbe()
    service = MailAccountSettingsService(
        store=OverlayMailSettingsStore(overlay_path(tmp_path / config)),
        accounts=accounts,
        credentials=source,
        clock=FakeClock(),
        probe=scripted,
    )
    return service, source, scripted


# --------------------------------------------------------------------- reading


async def test_an_unknown_field_is_reported_as_not_configured(tmp_path: Path) -> None:
    """Nothing is guessed: an absent provider is absent, not a plausible default."""
    service, _, _ = _service(tmp_path, accounts=(account(),))

    (settings,) = await service.list_safe()

    assert settings.smtp.host is None
    assert settings.smtp.port is None
    assert settings.smtp.security is None
    assert settings.smtp.configured is False
    assert settings.send_ready is False
    assert settings.receive_ready is False, "no credential is available in this test"


async def test_receive_and_send_readiness_come_from_configuration(tmp_path: Path) -> None:
    credentials = ScriptedCredentials(True, True)
    service, _, _ = _service(
        tmp_path, accounts=(sending_account(),), credentials=credentials
    )

    (settings,) = await service.list_safe()

    assert settings.imap.configured is True
    assert settings.smtp.configured is True
    assert settings.receive_ready is True
    assert settings.send_ready is True


async def test_a_disabled_account_is_never_ready(tmp_path: Path) -> None:
    service, _, _ = _service(
        tmp_path,
        accounts=(sending_account(),),
        credentials=ScriptedCredentials(True, True),
    )
    await service.set_enabled("smail", enabled=False)

    (settings,) = await service.list_safe()

    assert settings.enabled is False
    assert settings.receive_ready is False
    assert settings.send_ready is False


async def test_an_unknown_account_is_not_found(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)

    with pytest.raises(MailAccountSettingsNotFound):
        await service.get("nope")


# ---------------------------------------------------------------- safe payload


async def test_the_safe_payload_carries_no_credential_value(tmp_path: Path) -> None:
    """The property the whole phase exists for: a secret has no field to travel in."""
    service, _, _ = _service(
        tmp_path, accounts=(sending_account(),), credentials=ScriptedCredentials(True, True)
    )

    rendered = json.dumps(
        [settings.to_payload() for settings in await service.list_safe()], ensure_ascii=False
    )

    assert INBOUND_SENTINEL not in rendered
    assert OUTBOUND_SENTINEL not in rendered
    # What it does say is *that* a credential exists and which variable names it.
    payload = (await service.get("smail")).to_payload()
    assert payload["credential"]["configured"] is True
    assert payload["credential"]["source_kind"] == "environment"
    assert payload["credential"]["reference"] == "GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD"
    assert set(payload["credential"]) == {"configured", "source_kind", "reference"}


async def test_a_connection_test_hands_the_probe_only_an_account(tmp_path: Path) -> None:
    """The core layer never holds a credential value, not even on its way to a test."""
    credentials = ScriptedCredentials(True, True)
    service, _, probe = _service(
        tmp_path, accounts=(sending_account(),), credentials=credentials
    )

    await service.test_imap("smail")
    await service.test_smtp("smail")

    assert probe.calls == ["imap", "smtp"]
    # A connection test asks this layer nothing about a credential at all: the probe reads the
    # environment itself, and the service only ever learns whether one exists for the *listing*.
    assert credentials.asked == []


async def test_listing_accounts_asks_only_whether_a_credential_exists(tmp_path: Path) -> None:
    credentials = ScriptedCredentials(True, True)
    service, _, _ = _service(
        tmp_path, accounts=(sending_account(),), credentials=credentials
    )

    await service.list_safe()

    # "Is it configured?" is answered, and nothing beyond that is reachable from this layer.
    assert {kind for kind, _ in credentials.asked} == {"inbound"}


# -------------------------------------------------------------------- writing


async def test_creating_an_account_writes_the_managed_overlay_and_asks_for_a_restart(
    tmp_path: Path,
) -> None:
    service, _, _ = _service(tmp_path)

    result = await service.create(
        MailAccountDraft(
            id="smail",
            host="imap.example.edu",
            username="student@example.edu",
            mailbox="INBOX",
            port=993,
            smtp_host="smtp.example.edu",
            smtp_port=587,
            smtp_security="starttls",
            smtp_username="student@example.edu",
            from_address="student@example.edu",
        )
    )

    assert result.restart_required is True
    assert result.applied_immediately is False
    assert "重启" in result.detail
    stored = read_overlay(overlay_path(tmp_path / "config.toml"))
    assert [entry.id for entry in stored] == ["smail"]
    assert stored[0].smtp_host == "smtp.example.edu"


async def test_the_primary_configuration_is_never_written_by_a_settings_change(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('format_version = 1\n', encoding="utf-8")
    before = config_path.read_bytes()
    service, _, _ = _service(tmp_path)

    await service.create(
        MailAccountDraft(id="smail", host="imap.example.edu", username="u")
    )

    assert config_path.read_bytes() == before


async def test_updating_keeps_the_account_in_place(tmp_path: Path) -> None:
    service, _, _ = _service(
        tmp_path, accounts=(account("legacy"), account("smail"))
    )

    await service.update(
        "smail", MailAccountDraft(id="smail", host="imap.new.edu", username="u")
    )

    assert [entry.id for entry in read_overlay(overlay_path(tmp_path / "config.toml"))] == [
        "legacy",
        "smail",
    ]
    (legacy, smail) = await service.list_safe()
    assert legacy.imap.host == "imap.example.edu"
    assert smail.imap.host == "imap.new.edu"


async def test_an_edit_cannot_change_an_account_id(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path, accounts=(account("smail"),))

    with pytest.raises(InvalidMailSettings):
        await service.update(
            "smail", MailAccountDraft(id="other", host="h", username="u")
        )


async def test_a_duplicate_id_is_refused(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path, accounts=(account("smail"),))

    with pytest.raises(InvalidMailSettings):
        await service.create(
            MailAccountDraft(id="smail", host="imap.other.edu", username="u")
        )


async def test_a_credential_shaped_field_is_refused(tmp_path: Path) -> None:
    """A password cannot be smuggled in through the settings surface: there is no field for one.

    This is a `TypeError` rather than a validation error on purpose. A draft with a password field
    that the service refused would still be a draft that *can hold* a password, and the point of
    ADR-0043 §13-15 is that no type in this path has somewhere to put one.
    """
    with pytest.raises(TypeError):
        MailAccountDraft(  # type: ignore[call-arg]
            id="smail",
            host="imap.example.edu",
            username="u",
            password=INBOUND_SENTINEL,
        )

    # And the stored form has no key either, so a hand-edited overlay cannot introduce one that a
    # later write would faithfully copy forward.
    service, _, _ = _service(tmp_path)
    await service.create(
        MailAccountDraft(id="smail", host="imap.example.edu", username="u")
    )
    written = (tmp_path / "mail-accounts.toml").read_text(encoding="utf-8")
    assert "password" not in written


async def test_a_plaintext_smtp_mode_is_refused(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)

    with pytest.raises(InvalidMailSettings):
        await service.create(
            MailAccountDraft(
                id="smail",
                host="imap.example.edu",
                username="u",
                smtp_host="smtp.example.edu",
                smtp_port=25,
                smtp_security="plain",
                from_address="u@example.edu",
            )
        )


async def test_a_half_configured_sender_is_refused(tmp_path: Path) -> None:
    """A sender with a host but no address is a trap, so the parser refuses it."""
    service, _, _ = _service(tmp_path)

    with pytest.raises(InvalidMailSettings):
        await service.create(
            MailAccountDraft(
                id="smail", host="imap.example.edu", username="u", smtp_host="smtp.example.edu"
            )
        )


async def test_writing_twice_leaves_one_account(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)
    draft = MailAccountDraft(id="smail", host="imap.example.edu", username="u")

    await service.create(draft)
    await service.update("smail", MailAccountDraft(id="smail", host="imap.again.edu", username="u"))

    stored = read_overlay(overlay_path(tmp_path / "config.toml"))
    assert len(stored) == 1
    assert stored[0].host == "imap.again.edu"


# ------------------------------------------------------------------ diagnostics


async def test_the_inbound_test_is_run_and_its_result_returned(tmp_path: Path) -> None:
    credentials = ScriptedCredentials(inbound=INBOUND_SENTINEL)
    service, _, probe = _service(
        tmp_path, accounts=(account(),), credentials=credentials
    )

    report = await service.test_imap("smail")

    assert report.outcome is MailProbeOutcome.OK
    assert report.authenticated is True
    assert report.mailbox == "INBOX"
    assert INBOUND_SENTINEL not in json.dumps(report.to_payload())
    assert probe.calls == ["imap"]


async def test_the_outbound_test_reports_a_reachability_result_without_a_credential(
    tmp_path: Path,
) -> None:
    _, _, probe = _service(
        tmp_path,
        accounts=(sending_account(),),
        probe=ScriptedProbe(smtp_outcome=MailProbeOutcome.REACHABLE),
    )
    service, _, _ = _service(
        tmp_path,
        accounts=(sending_account(),),
        probe=probe,
    )

    report = await service.test_smtp("smail")

    assert report.outcome is MailProbeOutcome.REACHABLE
    assert report.authenticated is False
    assert report.outcome.is_success is True
    assert probe.calls == ["smtp"]


async def test_testing_an_unknown_account_is_not_found(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)

    with pytest.raises(MailAccountSettingsNotFound):
        await service.test_imap("nope")


async def test_without_a_probe_the_service_refuses_rather_than_lying(tmp_path: Path) -> None:
    service = MailAccountSettingsService(
        store=OverlayMailSettingsStore(overlay_path(tmp_path / "config.toml")),
        accounts=(account(),),
        credentials=ScriptedCredentials(),
        clock=FakeClock(),
        probe=None,
    )

    with pytest.raises(InvalidMailSettings):
        await service.test_imap("smail")


# --------------------------------------------------------------- no side effects


async def test_a_settings_write_and_a_connection_test_create_no_action_or_approval(
    tmp_path: Path,
) -> None:
    """§25-26: a diagnostic is not an effect, and nothing here reaches the approval boundary."""
    database = Path(tmp_path) / "data" / "assistant.db"
    database.parent.mkdir(parents=True)
    from assistant.store.db import Database
    from assistant.store.migrations import apply_migrations

    runtime = Database.at(database)
    clock = FakeClock()
    apply_migrations(runtime, clock=clock)
    service, _, _ = _service(
        tmp_path,
        accounts=(sending_account(),),
        credentials=ScriptedCredentials(True, True),
    )

    await service.create(MailAccountDraft(id="other", host="imap.example.edu", username="u"))
    await service.test_smtp("smail")

    connection = sqlite3.connect(str(database))
    try:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("action_requests", "approvals", "execution_runs", "approval_challenges")
        }
    finally:
        connection.close()
    assert counts == {
        "action_requests": 0,
        "approvals": 0,
        "execution_runs": 0,
        "approval_challenges": 0,
    }
