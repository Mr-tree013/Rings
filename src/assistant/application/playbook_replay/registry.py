"""The replay capability set: two validators, registered by name (ADR-0028).

This registry is the playbook equivalent of the executor set, and it is deliberately just as
explicit. There is no dynamic import, no plugin discovery and no entry-point loading: the two
validators are constructed here, and an action type without one cannot become a candidate at all.

That is also why an unknown type is refused *early*. A pending candidate that no validator can
ever test would be a review queue item with no possible outcome, so creation fails with
`PlaybookSourceUnsupported` instead.
"""

from __future__ import annotations

from collections.abc import Iterable

from assistant.application.playbook_replay.ehall_certificate import (
    EHallCertificateReplayValidator,
)
from assistant.application.playbook_replay.mail_send import MailSendReplayValidator
from assistant.domain.action import ActionType
from assistant.domain.errors import PlaybookReplayUnsupported
from assistant.ports.playbook_replay import PlaybookReplayValidator


class PlaybookReplayRegistry:
    """Maps one action type to the one validator that can dry-run it."""

    def __init__(
        self, validators: Iterable[PlaybookReplayValidator] = ()
    ) -> None:
        registry: dict[str, PlaybookReplayValidator] = {}
        for validator in validators:
            key = validator.action_type.value
            if key in registry:
                raise ValueError(f"two replay validators were registered for {key}")
            if validator.contract_version < 1:
                raise ValueError(
                    f"replay validator {key} declares a non-positive contract version"
                )
            registry[key] = validator
        self._validators = registry

    @classmethod
    def default(cls) -> PlaybookReplayRegistry:
        """The production capability set: the two action types this project implements."""
        return cls([MailSendReplayValidator(), EHallCertificateReplayValidator()])

    @property
    def action_types(self) -> tuple[ActionType, ...]:
        """Every action type a dry run can validate, in a stable order."""
        return tuple(ActionType(key) for key in sorted(self._validators))

    def supports(self, action_type: ActionType) -> bool:
        """Whether this deployment can dry-run that action type."""
        return action_type.value in self._validators

    def validator_for(self, action_type: ActionType) -> PlaybookReplayValidator:
        """Return the validator for one action type.

        Raises:
            PlaybookReplayUnsupported: no validator is registered for this type.
        """
        validator = self._validators.get(action_type.value)
        if validator is None:
            raise PlaybookReplayUnsupported(action_type)
        return validator


__all__ = ["PlaybookReplayRegistry"]
