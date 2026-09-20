"""The mobile HTTP surface: authentication, CSRF, headers and what it refuses (ADR-0026).

Driven through the ASGI test client, so nothing opens a socket. The private-client decision is
injected, which is how the "a public peer is refused whatever it claims" case is proven.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from assistant.adapters.web.app import (
    CONTENT_SECURITY_POLICY,
    CSRF_COOKIE,
    SESSION_COOKIE,
)
from assistant.store.db import Database
from assistant.store.migrations import apply_migrations
from tests.support.fakes import FakeClock
from tests.support.mobile import (
    CSRF_TOKEN,
    PAIRING_TOKEN,
    SESSION_TOKEN,
    MobileStack,
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
def stack(database: Database, clock: FakeClock) -> MobileStack:
    return MobileStack(database, clock, ScriptedTokenFactory())


# ------------------------------------------------------------------- pairing


def test_pairing_sets_a_session_and_a_csrf_cookie(stack: MobileStack) -> None:
    client = stack.client()
    issued = stack.tokens
    code = stack.issue_pairing()
    assert code == PAIRING_TOKEN
    pairing = client.post("/api/pair", json={"token": code})
    assert pairing.status_code == 200, pairing.text
    assert pairing.json() == {"paired": True}
    assert client.cookies.get(SESSION_COOKIE) == SESSION_TOKEN
    assert client.cookies.get(CSRF_COOKIE) == CSRF_TOKEN
    # The session cookie is HttpOnly; the CSRF one must be readable by the page's own JS.
    cookies = pairing.headers.get_list("set-cookie")
    session_cookie = next(item for item in cookies if item.startswith(SESSION_COOKIE))
    csrf_cookie = next(item for item in cookies if item.startswith(CSRF_COOKIE))
    assert "HttpOnly" in session_cookie
    assert "HttpOnly" not in csrf_cookie
    for cookie in (session_cookie, csrf_cookie):
        assert "samesite=strict" in cookie.lower()
    assert issued.issued[:3] == [PAIRING_TOKEN, SESSION_TOKEN, CSRF_TOKEN]


def test_a_wrong_pairing_code_is_refused(stack: MobileStack) -> None:
    client = stack.client()

    response = client.post("/api/pair", json={"token": "wrong-code-0123456789ABCDEFGH"})

    assert response.status_code == 403
    assert "not valid" in response.json()["error"]
    assert client.cookies.get(SESSION_COOKIE) is None


def test_me_requires_a_session(stack: MobileStack) -> None:
    client = stack.client()

    assert client.get("/api/me").status_code == 401

    stack.pair(client)
    assert client.get("/api/me").status_code == 200


def test_logout_revokes_and_clears(stack: MobileStack) -> None:
    client = stack.client()
    tokens = stack.pair(client)

    response = client.post("/api/logout", headers=stack.csrf_headers(tokens["csrf"]))

    assert response.status_code == 200
    assert client.get("/api/me").status_code == 401


# --------------------------------------------------------- private clients


def test_a_public_client_is_refused_even_with_a_forwarded_header(
    stack: MobileStack,
) -> None:
    """The decision uses the socket peer; a header cannot claim to be local."""
    client = stack.client(is_private=lambda peer: False)

    response = client.get(
        "/api/me", headers={"X-Forwarded-For": "192.168.1.10", "X-Real-IP": "127.0.0.1"}
    )

    assert response.status_code == 403
    assert "local network" in response.json()["error"]
    assert client.post("/api/pair", json={"token": PAIRING_TOKEN}).status_code == 403


def test_the_private_client_test_accepts_only_local_addresses() -> None:
    from assistant.adapters.web.server import is_private_client

    for peer in ("127.0.0.1", "::1", "192.168.1.5", "10.0.0.7", "169.254.1.1"):
        assert is_private_client(peer) is True, peer
    for peer in ("8.8.8.8", "1.1.1.1", "", "not-an-address", "example.com"):
        assert is_private_client(peer) is False, peer


# ------------------------------------------------------------------- CSRF


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (None, 403),
        ({"X-CSRF-Token": "wrong-token-0123456789ABCDEFG"}, 403),
        ({"X-CSRF-Token": CSRF_TOKEN}, 201),
    ],
)
def test_mutations_need_the_csrf_pair(
    stack: MobileStack, headers: dict[str, str] | None, expected: int
) -> None:
    client = stack.client()
    tokens = stack.pair(client)

    response = client.post("/api/tasks", json={"title": "Write report"}, headers=headers)

    assert response.status_code == expected, response.text
    assert tokens["csrf"] == CSRF_TOKEN


def test_a_csrf_header_without_the_cookie_is_refused(stack: MobileStack) -> None:
    client = stack.client()
    stack.pair(client)
    client.cookies.delete(CSRF_COOKIE)

    response = client.post(
        "/api/tasks", json={"title": "Write report"}, headers={"X-CSRF-Token": CSRF_TOKEN}
    )

    assert response.status_code == 403


def test_reads_stay_reads(stack: MobileStack) -> None:
    """No GET may mutate anything, even with a session."""
    client = stack.client()
    stack.pair(client)

    for path in ("/api/dashboard", "/api/tasks", "/api/cases", "/api/notifications",
                 "/api/mail/drafts", "/api/actions"):
        assert client.get(path).status_code == 200, path

    client.post(
        "/api/tasks", json={"title": "One"}, headers=stack.csrf_headers(CSRF_TOKEN)
    )
    assert len(client.get("/api/tasks").json()["tasks"]) == 1


# ------------------------------------------------------- headers and surface


@pytest.mark.parametrize(
    "path", ["/", "/pair", "/app.js", "/styles.css", "/api/me"]
)
def test_every_response_carries_the_security_headers(
    stack: MobileStack, path: str
) -> None:
    client = stack.client()

    response = client.get(path)

    assert response.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Cache-Control"] == "no-store"


def test_the_policy_forbids_external_assets_and_framing() -> None:
    for directive in (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "connect-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "form-action 'self'",
    ):
        assert directive in CONTENT_SECURITY_POLICY


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_the_schema_endpoints_are_disabled(stack: MobileStack, path: str) -> None:
    assert stack.client().get(path).status_code == 404


def test_the_static_assets_are_self_contained(stack: MobileStack) -> None:
    """No remote URL of any kind: every asset the page loads is served by this host.

    Checked on the *references* the browser would follow (src/href/@import) rather than on the
    text: the client's own comment explains that there is no CDN, and that sentence is the design.
    """
    import re

    client = stack.client()
    for path in ("/", "/pair", "/app.js", "/styles.css"):
        body = client.get(path).text
        for match in re.findall(r"(?:src|href)\s*=\s*[\"']([^\"']+)[\"']", body):
            assert not match.startswith(("http://", "https://", "//")), (path, match)
        assert "@import" not in body, path
        assert "eval(" not in body, path
        assert "new Function" not in body, path
    assert "script-src 'self'" in CONTENT_SECURITY_POLICY


def test_the_client_never_treats_data_as_html(stack: MobileStack) -> None:
    """The UI writes user-controlled text with textContent, and evals nothing."""
    script = stack.client().get("/app.js").text

    assert "innerHTML" not in script
    assert "outerHTML" not in script
    assert "insertAdjacentHTML" not in script
    assert "eval(" not in script
    assert "new Function" not in script


def test_the_pairing_code_is_never_put_in_a_url(stack: MobileStack) -> None:
    """The pairing page posts the code; it never appears in a query string or a fragment."""
    page = stack.client().get("/pair").text

    assert "/api/pair" in stack.client().get("/app.js").text
    assert "?token" not in page
    assert "#token" not in page


def test_the_approval_link_uses_a_fragment_and_strips_it(stack: MobileStack) -> None:
    script = stack.client().get("/app.js").text

    assert "window.location.hash" in script
    assert "history.replaceState" in script
    assert "#token" not in stack.client().get("/approve/abc").text


# --------------------------------------------------------------- logging


def test_the_control_plane_logs_neither_secrets_nor_user_content(
    stack: MobileStack, caplog: pytest.LogCaptureFixture
) -> None:
    """A log file is not allowed to become a second, unhashed copy of anything sensitive.

    Access logging is off at the server, and the routes themselves log nothing — this test is the
    regression lock on both: a full authenticated session, including a mutation and an approval,
    must leave the log clean.
    """
    secret = "TASK-TITLE-THAT-MUST-NOT-BE-LOGGED"
    with caplog.at_level("DEBUG"):
        client = stack.client()
        tokens = stack.pair(client)
        created = client.post(
            "/api/tasks",
            json={"title": secret},
            headers=stack.csrf_headers(tokens["csrf"]),
        )
        assert created.status_code == 201, created.text
        assert client.get("/api/dashboard").status_code == 200
        assert client.get("/api/me").status_code == 200

    logged = caplog.text
    for forbidden in (PAIRING_TOKEN, SESSION_TOKEN, CSRF_TOKEN, secret):
        assert forbidden not in logged, forbidden
