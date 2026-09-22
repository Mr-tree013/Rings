"""The settings HTTP surface: typed mail account metadata and connectivity tests (ADR-0043).

```text
GET   /settings                                  the page (static, authenticated shell)
GET   /api/settings/mail/accounts                safe views, never a secret
POST  /api/settings/mail/accounts                add one account
PATCH /api/settings/mail/accounts/{id}           edit one account
POST  /api/settings/mail/accounts/{id}/test-imap read-only inbound diagnostic
POST  /api/settings/mail/accounts/{id}/test-smtp connect + auth + NOOP, never DATA
```

What is deliberately absent is the point of the module. There is **no** route that returns a
password or a secret, **no** route named `send-test-message`, and **no** route that creates an
`ActionRequest`, an `Approval` or an `ExecutionRun`. A connectivity test is a diagnostic; sending
mail still requires the conversation's exact-review path.

Every handler is a thin translation: it validates a bounded JSON body, calls
`MailAccountSettingsService`, and returns a safe payload. The handler never writes a file, never
constructs TOML and never touches a credential: the browser sends typed fields and the application
layer owns every rule about what may be stored.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from assistant.application.mail_account_settings import (
    MailAccountDraft,
    MailAccountSettingsService,
)
from assistant.domain.errors import (
    InvalidMailSettings,
    MailAccountSettingsNotFound,
)
from assistant.domain.mobile import MobileWebSession

MAX_BODY_BYTES = 16 * 1024
"""A settings form is a handful of short strings; anything larger is a mistake."""

MAX_FIELD_CHARS = 320
"""Long enough for an address, short enough that nothing here can become a payload channel."""


def register_settings_routes(
    app: FastAPI,
    *,
    settings: Callable[[], MailAccountSettingsService] | None,
    assets: object,
    require_session: Callable[[Request], Awaitable[MobileWebSession | JSONResponse]],
    require_mutation: Callable[[Request], Awaitable[MobileWebSession | JSONResponse]],
) -> None:
    """Register the settings surface, reusing the control plane's existing auth pair."""

    @app.get("/settings")
    async def settings_page() -> FileResponse:
        return FileResponse(assets / "settings.html", media_type="text/html")  # type: ignore[operator]

    @app.get("/settings.js")
    async def settings_script() -> FileResponse:
        return FileResponse(
            assets / "settings.js",  # type: ignore[operator]
            media_type="application/javascript",
        )

    @app.get("/settings.css")
    async def settings_styles() -> FileResponse:
        return FileResponse(assets / "settings.css", media_type="text/css")  # type: ignore[operator]

    if settings is None:
        return

    @app.get("/api/settings/mail/accounts")
    async def list_accounts(request: Request) -> JSONResponse:
        session = await require_session(request)
        if isinstance(session, JSONResponse):
            return session
        accounts = await settings().list_safe()
        return JSONResponse(
            {
                "accounts": [account.to_payload() for account in accounts],
                "restart_required": False,
                "detail": "",
            }
        )

    @app.post("/api/settings/mail/accounts")
    async def create_account(request: Request) -> JSONResponse:
        session = await require_mutation(request)
        if isinstance(session, JSONResponse):
            return session
        body = await _json_body(request)
        if body is None:
            return _json_error(413, "这个请求比设置页面能接受的要大。")
        draft = _draft(body.get("account"))
        if draft is None:
            return _json_error(400, "请填写账号 id、收件服务器和用户名。")
        service = settings()
        try:
            result = await service.create(draft)
        except InvalidMailSettings as exc:
            return _json_error(400, str(exc))
        return JSONResponse(result.to_payload(), status_code=201)

    @app.patch("/api/settings/mail/accounts/{account_id}")
    async def update_account(account_id: str, request: Request) -> JSONResponse:
        session = await require_mutation(request)
        if isinstance(session, JSONResponse):
            return session
        body = await _json_body(request)
        if body is None:
            return _json_error(413, "这个请求比设置页面能接受的要大。")
        draft = _draft(body.get("account"), account_id=account_id)
        if draft is None:
            return _json_error(400, "请填写账号 id、收件服务器和用户名。")
        try:
            result = await settings().update(account_id, draft)
        except MailAccountSettingsNotFound:
            return _json_error(404, "找不到这个邮箱账号。")
        except InvalidMailSettings as exc:
            return _json_error(400, str(exc))
        return JSONResponse(result.to_payload())

    @app.post("/api/settings/mail/accounts/{account_id}/test-imap")
    async def test_imap(account_id: str, request: Request) -> JSONResponse:
        return await _test(
            request,
            account_id=account_id,
            outbound=False,
            settings=settings,
            require_mutation=require_mutation,
        )

    @app.post("/api/settings/mail/accounts/{account_id}/test-smtp")
    async def test_smtp(account_id: str, request: Request) -> JSONResponse:
        return await _test(
            request,
            account_id=account_id,
            outbound=True,
            settings=settings,
            require_mutation=require_mutation,
        )


async def _test(
    request: Request,
    *,
    account_id: str,
    outbound: bool,
    settings: Callable[[], MailAccountSettingsService],
    require_mutation: Callable[[Request], Awaitable[MobileWebSession | JSONResponse]],
) -> JSONResponse:
    """Run one diagnostic. It reports what happened and never sends anything."""
    session = await require_mutation(request)
    if isinstance(session, JSONResponse):
        return session
    service = settings()
    try:
        report = (
            await service.test_smtp(account_id) if outbound else await service.test_imap(account_id)
        )
    except MailAccountSettingsNotFound:
        return _json_error(404, "找不到这个邮箱账号。")
    except InvalidMailSettings as exc:
        return _json_error(409, str(exc))
    return JSONResponse(report.to_payload())


def _draft(value: object, *, account_id: str | None = None) -> MailAccountDraft | None:
    """Read a bounded, typed account from a JSON object, or `None` when it is not one."""
    if not isinstance(value, dict):
        return None
    identifier = account_id if account_id is not None else _text(value.get("id"))
    host = _text(value.get("host"))
    username = _text(value.get("username"))
    if not identifier or not host or not username:
        return None
    port = _integer(value.get("port"), default=993)
    if port is None:
        return None
    sent_mailbox = _text(value.get("sent_mailbox"))
    return MailAccountDraft(
        id=identifier,
        host=host,
        username=username,
        mailbox=_text(value.get("mailbox")) or "INBOX",
        port=port,
        enabled=value.get("enabled") is not False,
        smtp_host=_text(value.get("smtp_host")),
        smtp_port=_optional_integer(value.get("smtp_port")),
        smtp_security=_text(value.get("smtp_security")),
        smtp_username=_text(value.get("smtp_username")),
        from_address=_text(value.get("from_address")),
        sent_mailbox=sent_mailbox,
    )


def _text(value: object) -> str | None:
    """A bounded, non-blank string, or `None`. Everything the browser sends is untrusted."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > MAX_FIELD_CHARS:
        return None
    return cleaned


def _integer(value: object, *, default: int) -> int | None:
    """A port, or `None` when the value is not a usable one."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= 65535 else None


def _optional_integer(value: object) -> int | None:
    """A port that may be absent, but is never invented."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= 65535 else None


async def _json_body(request: Request) -> dict[str, object] | None:
    """Read a small JSON object, or `None` when the body is over the ceiling."""
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return None
    if not raw:
        return {}
    with contextlib.suppress(json.JSONDecodeError):
        decoded = json.loads(raw)
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _json_error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


__all__ = ["MAX_BODY_BYTES", "MAX_FIELD_CHARS", "register_settings_routes"]
