"""Human approval of one exact action fingerprint (ADR-0023).

```text
create_challenge(action)      approve(action, token)
        │                             │
  load PREPARED action          one transaction:
  re-hash the payload           ├─ challenge: hash matches, unexpired, unconsumed
  mint a 256-bit token          ├─ action: still PREPARED, fingerprint re-verified
  store only its SHA-256        ├─ supersede an expired outstanding approval
  return the token once         ├─ consume the challenge
        │                       └─ insert the ApprovalRecord
        ▼                             │
  plaintext exists only here ─────────┘
```

Two rules are the reason this is a separate service rather than a flag on something else:

- **only a person approves.** Nothing in this module imports a model, a prompt, an event handler
  or a mail path, and the only caller is the CLI command the user types;
- **an approval is bound to bytes, not to an intention.** The fingerprint is re-derived from the
  stored payload at both ends of the flow, so an action whose content changed between the
  challenge and the approval cannot be approved by the old token, and cannot be executed by the
  old approval.

The token factory is injected, never imported: the domain and the application must not be able to
decide how a secret is generated.
"""

from __future__ import annotations

from collections.abc import Callable

from assistant.domain.action import ActionRequest, ActionRequestId
from assistant.domain.approval import (
    ApprovalChallenge,
    ApprovalChallengeIssued,
    ApprovalRecord,
    approval_ttl,
    hash_approval_token,
    new_approval_challenge_id,
    new_approval_id,
)
from assistant.domain.errors import (
    ActionFingerprintMismatch,
    ActionNotExecutable,
    ActionRequestNotFound,
    ApprovalChallengeNotFound,
    InvalidApproval,
)
from assistant.ports.action_repository import ActionRepository
from assistant.ports.clock import Clock


class ApprovalService:
    """Issues single-use challenges and redeems them into approvals."""

    def __init__(
        self,
        actions: ActionRepository,
        clock: Clock,
        *,
        token_factory: Callable[[], str],
    ) -> None:
        self._actions = actions
        self._clock = clock
        self._token_factory = token_factory

    async def create_challenge(self, action_id: ActionRequestId) -> ApprovalChallengeIssued:
        """Mint one short-lived challenge for one PREPARED action.

        The action is re-verified first: a challenge is never issued for content that no longer
        hashes to its fingerprint, and never for an action that is already terminal.

        Raises:
            ActionRequestNotFound: no such action.
            ActionFingerprintMismatch: the payload does not match its fingerprint.
            ActionNotExecutable: the action is not PREPARED.
        """
        action = await self._load(action_id)
        token = self._token_factory()
        if not token:
            raise InvalidApproval("the token factory returned an empty token")
        now = self._clock.now()
        stored = await self._actions.add_challenge(
            ApprovalChallenge(
                id=new_approval_challenge_id(),
                action_id=action.id,
                action_fingerprint=action.fingerprint,
                token_hash=hash_approval_token(token),
                created_at=now,
                expires_at=now + approval_ttl(),
            )
        )
        # The plaintext token leaves this function exactly once, in this object.
        return ApprovalChallengeIssued(challenge=stored, token=token)

    async def approve(self, action_id: ActionRequestId, token: str) -> ApprovalRecord:
        """Redeem one challenge into an approval bound to the action's exact fingerprint.

        Raises:
            ActionRequestNotFound: no such action.
            ApprovalChallengeNotFound: no open challenge exists for this action.
            InvalidApprovalToken: the token is not the one that was issued.
            ApprovalChallengeExpired: the challenge is past its expiry.
            ApprovalChallengeConsumed: the challenge was already redeemed.
            ApprovalAlreadyOutstanding: a live approval already exists.
            ActionFingerprintMismatch: the action's content changed since the challenge.
            ActionNotExecutable: the action is not PREPARED.
        """
        action = await self._load(action_id)
        challenge = await self._actions.latest_challenge(action.id)
        if challenge is None:
            raise ApprovalChallengeNotFound(action_id)
        grant = await self._actions.redeem_challenge(
            challenge_id=challenge.id,
            token_hash=hash_approval_token(token),
            action_id=action.id,
            action_fingerprint=action.fingerprint,
            approval_id=new_approval_id(),
            now=self._clock.now(),
        )
        return grant.approval

    async def list_approvals(self, action_id: ActionRequestId) -> list[ApprovalRecord]:
        """Every approval ever recorded for an action, oldest first."""
        return await self._actions.list_approvals(action_id)

    async def _load(self, action_id: ActionRequestId) -> ActionRequest:
        """Load an action and re-verify that its payload still matches its fingerprint."""
        action = await self._actions.get_action(action_id)
        if action is None:
            raise ActionRequestNotFound(action_id)
        if not action.fingerprint_matches():
            raise ActionFingerprintMismatch(action.id)
        if not action.is_prepared:
            raise ActionNotExecutable(
                f"action request {action.id} is {action.status}, not prepared"
            )
        return action


__all__ = ["ApprovalService"]
