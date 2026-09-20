"""The replay validators a playbook dry run may use (ADR-0028).

Two capabilities exist in this project, so two validators exist. Each one is a pure re-parse of an
already-approved payload: `mail_send` re-runs the strict mail-send parser, `ehall_certificate`
re-runs the typed certificate parser. Neither knows how to reach a server, a browser or a
credential, and neither is an `ActionExecutor`.
"""

from assistant.application.playbook_replay.ehall_certificate import (
    EHALL_CERTIFICATE_REPLAY_CONTRACT_VERSION,
    EHallCertificateReplayValidator,
)
from assistant.application.playbook_replay.mail_send import (
    MAIL_SEND_REPLAY_CONTRACT_VERSION,
    MailSendReplayValidator,
)
from assistant.application.playbook_replay.registry import PlaybookReplayRegistry

__all__ = [
    "EHALL_CERTIFICATE_REPLAY_CONTRACT_VERSION",
    "MAIL_SEND_REPLAY_CONTRACT_VERSION",
    "EHallCertificateReplayValidator",
    "MailSendReplayValidator",
    "PlaybookReplayRegistry",
]
