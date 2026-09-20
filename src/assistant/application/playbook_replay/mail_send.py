"""Dry-run validation for `mail.send` (ADR-0028).

The whole validator is one line of substance: hand the payload back to `MailSendPayload`, the same
strict parser the executor uses before it talks to a server. If today's code accepts the exact
historical document — schema version, addresses, Message-ID, Date and reply-header shape — the
dry run passes.

What it deliberately does not do is anything that would make the answer stronger than it is: no
SMTP credential is read, no connection is opened, no Sent folder is searched, and no new
Message-ID is minted. A pass means "this payload is still structurally understood here", which is
exactly what a reviewer needs to know before promoting a reference, and nothing more.
"""

from __future__ import annotations

from assistant.domain.action import ActionRequest, ActionType
from assistant.domain.errors import InvalidMailSend
from assistant.domain.mail_send import MAIL_SEND_SCHEMA_VERSION, MailSendPayload
from assistant.domain.playbook import (
    ISSUE_ACTION_TYPE_MISMATCH,
    ISSUE_PAYLOAD_INVALID,
    ISSUE_SCHEMA_VERSION_UNSUPPORTED,
    ReplayValidationResult,
)

MAIL_SEND_ACTION_TYPE = ActionType("mail.send")
"""The capability this validator understands."""

MAIL_SEND_REPLAY_CONTRACT_VERSION = 1
"""Bump when `MailSendPayload.from_payload` starts accepting or rejecting something new."""


class MailSendReplayValidator:
    """Re-parses an approved mail-send payload without touching mail."""

    action_type = MAIL_SEND_ACTION_TYPE
    contract_version = MAIL_SEND_REPLAY_CONTRACT_VERSION

    def validate(self, action: ActionRequest) -> ReplayValidationResult:
        """Say whether the current parser still accepts this exact payload."""
        if action.action_type != MAIL_SEND_ACTION_TYPE:
            return ReplayValidationResult.fail(ISSUE_ACTION_TYPE_MISMATCH)
        payload = action.payload
        if not isinstance(payload, dict):
            return ReplayValidationResult.fail(ISSUE_PAYLOAD_INVALID)
        version = payload.get("schema_version")
        if (
            isinstance(version, int)
            and not isinstance(version, bool)
            and version != MAIL_SEND_SCHEMA_VERSION
        ):
            return ReplayValidationResult.fail(ISSUE_SCHEMA_VERSION_UNSUPPORTED)
        try:
            MailSendPayload.from_payload(payload)
        except (InvalidMailSend, ValueError):
            return ReplayValidationResult.fail(ISSUE_PAYLOAD_INVALID)
        return ReplayValidationResult.pass_()


__all__ = [
    "MAIL_SEND_ACTION_TYPE",
    "MAIL_SEND_REPLAY_CONTRACT_VERSION",
    "MailSendReplayValidator",
]
