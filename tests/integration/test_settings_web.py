"""The settings HTTP surface: authentication, CSRF, safe bodies and no secret routes (ADR-0043).

Every assertion here is about a boundary rather than a feature: the page is reachable, the writes
are guarded, the answers carry no value, and the routes that would be dangerous simply do not exist.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from assistant.adapters.web.app import build_app
from assistant.adapters.web.server import is_private_client
from tests.support.chat import ChatStack, build_chat

SETTINGS_READS = ("/settings", "/settings.js", "/settings.css")
SETTINGS_API_READS = ("/api/settings/mail/accounts",)


@pytest.fixture
async def stack(tmp_path: Path) -> ChatStack:
    return await build_chat(tmp_path)


async def test_the_settings_shell_and_assets_are_local_files(stack: ChatStack) -> None:
    with stack.client() as client:
        page = client.get("/settings")
        script = client.get("/settings.js")
        styles = client.get("/settings.css")

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "/settings.js" in page.text and "/settings.css" in page.text
    assert script.status_code == 200 and styles.status_code == 200
    for asset in (page.text, script.text, styles.text):
        assert "https://" not in asset
        assert "http://" not in asset
        assert "innerHTML" not in asset


async def test_the_page_says_that_no_password_is_stored_there(stack: ChatStack) -> None:
    with stack.client() as client:
        page = client.get("/settings")

    assert "不会在网页中保存密码" in page.text
    assert "凭据引用" in page.text
    # And no password input exists to type one into.
    assert 'type="password"' not in page.text


@pytest.mark.parametrize("route", SETTINGS_READS)
async def test_the_page_is_reachable_without_a_session_but_its_data_is_not(
    stack: ChatStack, route: str
) -> None:
    """The shell is static, exactly like `/chat`; everything behind it needs the session."""
    with stack.client() as client:
        assert client.get(route).status_code == 200
        assert client.get("/api/settings/mail/accounts").status_code == 401


async def test_reading_accounts_requires_the_existing_session(stack: ChatStack) -> None:
    with stack.client() as client:
        assert client.get("/api/settings/mail/accounts").status_code == 401
        tokens = await stack.pair(client)
        assert client.get("/api/settings/mail/accounts").status_code == 200
        assert tokens["csrf"]


async def test_a_write_requires_the_csrf_pair(stack: ChatStack) -> None:
    draft = {"account": {"id": "smail", "host": "imap.example.edu", "username": "u"}}
    with stack.client() as client:
        tokens = await stack.pair(client)

        before = client.get("/api/settings/mail/accounts").json()["accounts"]

        refused = client.post("/api/settings/mail/accounts", json=draft)
        assert refused.status_code == 403

        wrong = client.post(
            "/api/settings/mail/accounts",
            json=draft,
            headers={"X-CSRF-Token": "not-the-csrf-token"},
        )
        assert wrong.status_code == 403
        # Neither refusal changed anything: a rejected request has no effect at all.
        assert client.get("/api/settings/mail/accounts").json()["accounts"] == before
        assert not (stack.harness.tmp_path / "host" / "mail-accounts.toml").exists()
        assert tokens["session"]


async def test_an_account_round_trips_through_the_api(stack: ChatStack) -> None:
    draft = {
        "account": {
            "id": "other",
            "host": "imap.example.edu",
            "port": 993,
            "username": "student@example.edu",
            "mailbox": "INBOX",
            "smtp_host": "smtp.example.edu",
            "smtp_port": 587,
            "smtp_security": "starttls",
            "smtp_username": "student@example.edu",
            "from_address": "student@example.edu",
        }
    }
    with stack.client() as client:
        tokens = await stack.pair(client)
        created = client.post(
            "/api/settings/mail/accounts", json=draft, headers=stack.headers(tokens["csrf"])
        )

        assert created.status_code == 201
        body = created.json()
        assert body["restart_required"] is True
        assert body["applied_immediately"] is False
        assert "重启" in body["detail"]

        listed = client.get("/api/settings/mail/accounts").json()["accounts"]
        # The account that was already in config.toml keeps its place; the new one is appended.
        assert [entry["id"] for entry in listed] == ["smail", "other"]
        assert listed[1]["smtp"]["security"] == "starttls"
        # No credential exists in this host, so both directions are honestly not ready.
        assert listed[1]["credential"]["configured"] is False
        assert listed[1]["receive_ready"] is False
        assert listed[1]["send_ready"] is False


async def test_the_api_never_returns_a_secret_shaped_field(stack: ChatStack) -> None:
    """A response body is checked for the word itself, not only for a known sentinel."""
    with stack.client() as client:
        tokens = await stack.pair(client)
        changed = client.patch(
            "/api/settings/mail/accounts/smail",
            json={"account": {"id": "smail", "host": "imap.example.edu", "username": "u"}},
            headers=stack.headers(tokens["csrf"]),
        )
        assert changed.status_code == 200
        raw = client.get("/api/settings/mail/accounts").text

    payload = json.loads(raw)
    (entry,) = payload["accounts"]
    assert set(entry["credential"]) == {"configured", "source_kind", "reference"}

    # No *field* is credential-shaped. The one occurrence of the word "password" is the environment
    # variable name the page shows so a user knows where to put a secret: a name, not a value.
    assert not _keys_matching(payload, ("password", "secret", "token"))
    assert raw.lower().count("password") == 1
    assert "GROWING_ASSISTANT_MAIL_SMAIL_PASSWORD" in raw


def _keys_matching(value: object, needles: tuple[str, ...]) -> set[str]:
    """Every JSON key whose name looks credential-shaped, at any depth."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if any(needle in key.lower() for needle in needles):
                found.add(key)
            found |= _keys_matching(item, needles)
    elif isinstance(value, list):
        for item in value:
            found |= _keys_matching(item, needles)
    return found


async def test_an_unknown_browser_field_is_dropped_rather_than_stored(stack: ChatStack) -> None:
    with stack.client() as client:
        tokens = await stack.pair(client)
        client.patch(
            "/api/settings/mail/accounts/smail",
            json={
                "account": {
                    "id": "smail",
                    "host": "imap.example.edu",
                    "username": "u",
                    "password": "SENTINEL-SHOULD-NOT-SURVIVE",
                }
            },
            headers=stack.headers(tokens["csrf"]),
        )

    written = (stack.harness.tmp_path / "host" / "mail-accounts.toml").read_text(encoding="utf-8")
    assert "SENTINEL-SHOULD-NOT-SURVIVE" not in written
    assert "password" not in written


async def test_the_primary_configuration_is_untouched_by_a_settings_write(
    stack: ChatStack,
) -> None:
    config_path = stack.harness.tmp_path / "host" / "config.toml"
    before = config_path.read_bytes()
    with stack.client() as client:
        tokens = await stack.pair(client)
        response = client.patch(
            "/api/settings/mail/accounts/smail",
            json={"account": {"id": "smail", "host": "imap.edited.edu", "username": "u"}},
            headers=stack.headers(tokens["csrf"]),
        )
        assert response.status_code == 200

    assert config_path.read_bytes() == before
    assert (stack.harness.tmp_path / "host" / "mail-accounts.toml").is_file()


async def test_a_malformed_draft_is_a_product_error_not_a_traceback(stack: ChatStack) -> None:
    with stack.client() as client:
        tokens = await stack.pair(client)
        response = client.post(
            "/api/settings/mail/accounts",
            json={"account": {"id": "smail"}},
            headers=stack.headers(tokens["csrf"]),
        )

    assert response.status_code == 400
    assert "Traceback" not in response.text
    assert "password" not in response.text.lower()


async def test_testing_an_unknown_account_is_a_four_oh_four(stack: ChatStack) -> None:
    with stack.client() as client:
        tokens = await stack.pair(client)
        response = client.post(
            "/api/settings/mail/accounts/nope/test-imap", headers=stack.headers(tokens["csrf"])
        )

    assert response.status_code == 404
    assert "找不到" in response.json()["error"]


def test_the_settings_route_set_is_exactly_this_and_contains_no_secret_route() -> None:
    """The frozen surface: six routes, none of which can return a secret or send a message."""
    import asyncio
    import tempfile

    from tests.support.chat import build_chat

    with tempfile.TemporaryDirectory() as directory:
        stack = asyncio.run(build_chat(Path(directory)))
        app = build_app(stack.dependencies(), is_private=is_private_client)
    routes = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
        if "settings" in route.path
    }

    assert routes == {
        ("GET", "/settings"),
        ("GET", "/settings.js"),
        ("GET", "/settings.css"),
        ("GET", "/api/settings/mail/accounts"),
        ("POST", "/api/settings/mail/accounts"),
        ("PATCH", "/api/settings/mail/accounts/{account_id}"),
        ("POST", "/api/settings/mail/accounts/{account_id}/test-imap"),
        ("POST", "/api/settings/mail/accounts/{account_id}/test-smtp"),
        ("GET", "/api/settings/planning"),
    }
    joined = " ".join(path for _, path in routes)
    for forbidden in ("password", "secret", "send", "message", "execute", "approve", "submit"):
        assert forbidden not in joined
