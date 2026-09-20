"""PlaybookReplayValidator port: a pure re-reading of one historical payload (ADR-0028).

This is the *only* thing a playbook dry run may do: hand the exact approved `ActionRequest` to a
capability-specific validator that re-parses it with today's code and says whether it still fits.
It is deliberately not an `ActionExecutor`:

- an executor is called after a human approval has been consumed and may change the world;
- a validator is called without any approval, must not touch the network, a browser, SMTP, IMAP or
  the filesystem, and changes nothing at all.

Reusing the executor protocol here would be the shortcut that quietly turns "test this shape" into
"do this again", so the two protocols stay separate even though both are keyed by `ActionType`.

`contract_version` is what makes a pass mean something specific over time: a passing test is only
eligible for promotion while the registered validator still declares the same version, so changing
how a payload is parsed invalidates old passes instead of silently inheriting them.
"""

from __future__ import annotations

from typing import Protocol

from assistant.domain.action import ActionRequest, ActionType
from assistant.domain.playbook import ReplayValidationResult


class PlaybookReplayValidator(Protocol):
    """Re-parses one capability's payload. Pure, offline and side-effect-free."""

    @property
    def action_type(self) -> ActionType:
        """The single action type this validator understands."""
        ...

    @property
    def contract_version(self) -> int:
        """The version of the validation rules this validator applies."""
        ...

    def validate(self, action: ActionRequest) -> ReplayValidationResult:
        """Say whether the current code still accepts this exact payload.

        Implementations must be pure: they may read the action and nothing else — no credential, no
        configuration, no browser, no socket, no clock. A payload the current parser refuses is a
        `FAILED` result with bounded issue codes, not an exception; an exception means the program
        is broken, which is not the same thing as a payload that no longer parses.
        """
        ...


__all__ = ["PlaybookReplayValidator"]
