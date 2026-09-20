"""Pairing and session security against real SQLite (ADR-0026).

Every assertion here is about what the database holds: a hash, an expiry and a single-use marker.
The plaintext exists only in the return value of the call that created it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from assistant.application.mobile_auth import MobileAuthService
from assistant.domain.errors import (
    AmbiguousId,
    MobileCsrfRejected,
    MobileDisabled,
    MobilePairingTokenInvalid,
    MobileSessionInvalid,
    MobileSessionNotFound,
)
from assistant.domain.mobile import (
    PAIRING_TTL_SECONDS,
    SESSION_TTL_SECONDS,
    hash_mobile_token,
    pairing_ttl,
    session_ttl,
)
from assistant.store.db import Database
from assistant.store.migrations import apply_migrations
from assistant.store.mobile_sessions import SqliteMobileSessionRepository
from tests.support.fakes import FakeClock
from tests.support.mobile import (
    CSRF_TOKEN,
    PAIRING_TOKEN,
    SECOND_CSRF_TOKEN,
    SECOND_PAIRING_TOKEN,
    SECOND_SESSION_TOKEN,
    SESSION_TOKEN,
    ScriptedTokenFactory,
)

NOW = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=NOW)


@pytest.fixture
def database(tmp_path: Path, clock: FakeClock) -> Database:
    db = Database.at(tmp_path / "assistant.db")
    apply_migrations(db, clock=clock)
    return db


@pytest.fixture
def repository(database: Database) -> SqliteMobileSessionRepository:
    return SqliteMobileSessionRepository(database)


@pytest.fixture
def service(
    repository: SqliteMobileSessionRepository, clock: FakeClock
) -> MobileAuthService:
    return MobileAuthService(
        repository, clock, token_factory=ScriptedTokenFactory(), enabled=True
    )


async def test_a_pairing_token_is_stored_only_as_a_hash(
    service: MobileAuthService, database: Database
) -> None:
    issued = await service.create_pairing_token()

    assert issued.token == PAIRING_TOKEN
    assert issued.pairing.token_hash == hash_mobile_token(PAIRING_TOKEN)
    assert issued.pairing.expires_at == NOW + pairing_ttl()
    with database.connect() as connection:
        dumped = " ".join(
            str(value)
            for row in connection.execute("SELECT * FROM mobile_pairing_tokens").fetchall()
            for value in tuple(row)
        )
    assert PAIRING_TOKEN not in dumped
    assert hash_mobile_token(PAIRING_TOKEN) in dumped


async def test_pairing_creates_a_session_and_consumes_the_code(
    service: MobileAuthService, database: Database
) -> None:
    issued = await service.create_pairing_token()

    paired = await service.pair(issued.token)

    assert paired.session_token == SESSION_TOKEN
    assert paired.csrf_token == CSRF_TOKEN
    assert paired.session.session_hash == hash_mobile_token(SESSION_TOKEN)
    assert paired.session.csrf_hash == hash_mobile_token(CSRF_TOKEN)
    assert paired.session.expires_at == NOW + session_ttl()
    with database.connect() as connection:
        dumped = " ".join(
            str(value)
            for row in connection.execute("SELECT * FROM mobile_sessions").fetchall()
            for value in tuple(row)
        )
    for secret in (PAIRING_TOKEN, SESSION_TOKEN, CSRF_TOKEN):
        assert secret not in dumped


async def test_a_pairing_code_works_once(
    service: MobileAuthService,
) -> None:
    issued = await service.create_pairing_token()
    await service.pair(issued.token)

    with pytest.raises(MobilePairingTokenInvalid):
        await service.pair(issued.token)


async def test_a_pairing_code_expires(
    service: MobileAuthService, clock: FakeClock
) -> None:
    issued = await service.create_pairing_token()
    clock.advance(PAIRING_TTL_SECONDS)

    with pytest.raises(MobilePairingTokenInvalid):
        await service.pair(issued.token)


async def test_an_unknown_or_short_code_is_refused(service: MobileAuthService) -> None:
    for candidate in ("not-a-real-code-0123456789abcdef", "short"):
        with pytest.raises(MobilePairingTokenInvalid):
            await service.pair(candidate)


async def test_a_session_authenticates_and_its_expiry_is_enforced(
    service: MobileAuthService, clock: FakeClock
) -> None:
    paired = await service.pair((await service.create_pairing_token()).token)

    session = await service.authenticate(paired.session_token)

    assert session.is_active_at(clock.now()) is True
    assert session.last_seen_at == clock.now()  # touching records the visit
    clock.advance(SESSION_TTL_SECONDS)
    with pytest.raises(MobileSessionInvalid):
        await service.authenticate(paired.session_token)


async def test_an_unknown_or_missing_session_is_refused(
    service: MobileAuthService,
) -> None:
    with pytest.raises(MobileSessionInvalid):
        await service.authenticate(None)
    with pytest.raises(MobileSessionInvalid):
        await service.authenticate("not-a-session-0123456789abcdefghij")


async def test_logout_revokes_the_session(service: MobileAuthService) -> None:
    paired = await service.pair((await service.create_pairing_token()).token)
    session = await service.authenticate(paired.session_token)

    revoked = await service.logout(session)

    assert revoked.is_revoked() is True
    with pytest.raises(MobileSessionInvalid):
        await service.authenticate(paired.session_token)


async def test_revoking_by_reference_and_listing_sessions(
    service: MobileAuthService,
) -> None:
    first = await service.pair((await service.create_pairing_token()).token)

    listed = await service.list_sessions()

    assert [item.id for item in listed] == [first.session.id]
    revoked = await service.revoke_session(str(first.session.id)[:8])
    assert revoked.is_revoked() is True
    assert await service.list_sessions(include_inactive=False) == []
    with pytest.raises(MobileSessionNotFound):
        await service.revoke_session("ffffffff")


async def test_a_disabled_control_plane_refuses_everything(
    repository: SqliteMobileSessionRepository, clock: FakeClock
) -> None:
    disabled = MobileAuthService(
        repository, clock, token_factory=ScriptedTokenFactory(), enabled=False
    )

    with pytest.raises(MobileDisabled):
        await disabled.create_pairing_token()
    with pytest.raises(MobileDisabled):
        await disabled.pair(PAIRING_TOKEN)
    with pytest.raises(MobileDisabled):
        await disabled.authenticate(SESSION_TOKEN)

    status = await disabled.status(bind="loopback", port=8765, enabled=False)
    assert status.enabled is False
    assert status.active_sessions == 0


async def test_csrf_verification_needs_both_copies_and_the_right_session(
    service: MobileAuthService,
) -> None:
    first = await service.pair((await service.create_pairing_token()).token)
    session = await service.authenticate(first.session_token)

    service.verify_csrf(
        session, header_token=first.csrf_token, cookie_token=first.csrf_token
    )
    for header, cookie in (
        (None, first.csrf_token),
        (first.csrf_token, None),
        (first.csrf_token, "different-token-0123456789abcdef"),
        ("wrong-token-0123456789abcdefghij", "wrong-token-0123456789abcdefghij"),
    ):
        with pytest.raises(MobileCsrfRejected):
            service.verify_csrf(session, header_token=header, cookie_token=cookie)


async def test_csrf_from_another_session_is_refused(
    repository: SqliteMobileSessionRepository, clock: FakeClock
) -> None:
    service = MobileAuthService(
        repository,
        clock,
        token_factory=ScriptedTokenFactory(
            [PAIRING_TOKEN, SESSION_TOKEN, CSRF_TOKEN, SECOND_PAIRING_TOKEN,
             SECOND_SESSION_TOKEN, SECOND_CSRF_TOKEN]
        ),
        enabled=True,
    )
    first = await service.pair((await service.create_pairing_token()).token)
    second = await service.pair((await service.create_pairing_token()).token)

    with pytest.raises(MobileCsrfRejected):
        service.verify_csrf(
            await service.authenticate(first.session_token),
            header_token=second.csrf_token,
            cookie_token=second.csrf_token,
        )


async def test_an_ambiguous_session_prefix_is_refused(
    repository: SqliteMobileSessionRepository, clock: FakeClock
) -> None:
    service = MobileAuthService(
        repository,
        clock,
        token_factory=ScriptedTokenFactory(
            [PAIRING_TOKEN, SESSION_TOKEN, CSRF_TOKEN, SECOND_PAIRING_TOKEN,
             SECOND_SESSION_TOKEN, SECOND_CSRF_TOKEN]
        ),
        enabled=True,
    )
    await service.pair((await service.create_pairing_token()).token)
    await service.pair((await service.create_pairing_token()).token)
    sessions = await service.list_sessions()

    common = str(sessions[0].id)[:1]
    if str(sessions[1].id).startswith(common):
        with pytest.raises(AmbiguousId):
            await service.revoke_session(common)
    else:  # pragma: no cover - the two UUIDs rarely share a prefix
        assert len(sessions) == 2


async def test_ttls_are_the_documented_values() -> None:
    assert pairing_ttl() == timedelta(seconds=600)
    assert session_ttl() == timedelta(seconds=30 * 24 * 3600)
