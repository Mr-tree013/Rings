"""Playbooks: what a successful run teaches, once a person has reviewed and tested it (ADR-0028).

A playbook is the *shape* of an action that worked — a name, a note, the exact source action and
execution it came from, the fingerprint of that approved payload, and the replay contract version
its dry run passed against. It is deliberately not a recipe: there are no placeholders, no
parameter slots and no values copied out of the payload, because the moment a workflow becomes
parameterised it becomes a new action looking for an approval, and nothing in this phase may
create one.

Three values live here, in the order a person meets them:

- a **candidate** names a successful action and waits for review;
- a **replay test** is the record of one side-effect-free dry run: did the current local parser
  still accept this exact historical payload, under this contract version;
- a **playbook** is the reviewed, tested, retired-able reference that promotion produced.

What a passing dry run means is narrow, and the domain keeps it narrow: the current code still
understands the payload. It says nothing about credentials, about the university's page today,
about whether the recipient is still right, or about whether repeating the action is a good idea.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from assistant.domain.action import ActionRequestId, ActionType
from assistant.domain.errors import (
    InvalidPlaybook,
    InvalidPlaybookCandidate,
    InvalidPlaybookCandidateTransition,
    InvalidPlaybookReplayTest,
    InvalidPlaybookTransition,
)
from assistant.domain.execution import ExecutionRunId

PlaybookCandidateId = UUID
"""Stable identity of one reviewed candidate."""

PlaybookId = UUID
"""Stable identity of one playbook."""

PlaybookReplayTestId = UUID
"""Stable identity of one recorded dry run."""

CANDIDATE_NAME_MAX_LENGTH = 200
CANDIDATE_NOTE_MAX_LENGTH = 2000

FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")

ISSUE_PAYLOAD_INVALID = "payload-invalid"
ISSUE_SCHEMA_VERSION_UNSUPPORTED = "schema-version-unsupported"
ISSUE_ACTION_TYPE_MISMATCH = "action-type-mismatch"
"""The only issue codes a replay validator may report.

Bounded and machine-readable on purpose: a validator never gets to write prose about a payload it
did not like, because that prose would be a copy of the payload's contents in the audit table.
"""

REPLAY_ISSUE_CODES: frozenset[str] = frozenset(
    {
        ISSUE_ACTION_TYPE_MISMATCH,
        ISSUE_PAYLOAD_INVALID,
        ISSUE_SCHEMA_VERSION_UNSUPPORTED,
    }
)


def new_playbook_candidate_id() -> PlaybookCandidateId:
    """Generate a fresh candidate identity."""
    return uuid4()


def new_playbook_id() -> PlaybookId:
    """Generate a fresh playbook identity."""
    return uuid4()


def new_playbook_replay_test_id() -> PlaybookReplayTestId:
    """Generate a fresh replay-test identity."""
    return uuid4()


def validate_candidate_name(name: str) -> str:
    """Return the trimmed name, or raise `InvalidPlaybookCandidate`."""
    if not isinstance(name, str):
        raise InvalidPlaybookCandidate("a playbook name must be text")
    stripped = name.strip()
    if not stripped:
        raise InvalidPlaybookCandidate("a playbook name must not be blank")
    if len(stripped) > CANDIDATE_NAME_MAX_LENGTH:
        raise InvalidPlaybookCandidate(
            f"a playbook name must be at most {CANDIDATE_NAME_MAX_LENGTH} characters"
        )
    return stripped


def validate_candidate_note(note: str) -> str:
    """Return the trimmed note, or raise `InvalidPlaybookCandidate`."""
    if not isinstance(note, str):
        raise InvalidPlaybookCandidate("a playbook note must be text")
    stripped = note.strip()
    if not stripped:
        raise InvalidPlaybookCandidate("a playbook note must not be blank")
    if len(stripped) > CANDIDATE_NOTE_MAX_LENGTH:
        raise InvalidPlaybookCandidate(
            f"a playbook note must be at most {CANDIDATE_NOTE_MAX_LENGTH} characters"
        )
    return stripped


def _require_aware(value: datetime, field_name: str, error: type[Exception]) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise error(f"{field_name} must be timezone-aware")


def _require_optional_aware(
    value: datetime | None, field_name: str, error: type[Exception]
) -> None:
    if value is not None:
        _require_aware(value, field_name, error)


def _require_fingerprint(value: str, field_name: str, error: type[Exception]) -> None:
    if not FINGERPRINT_PATTERN.match(value):
        raise error(f"{field_name} must be 64 lowercase hex characters")


class PlaybookCandidateStatus(StrEnum):
    """Where a reviewed candidate stands. Every state but `PENDING` is terminal."""

    PENDING = "pending"
    PROMOTED = "promoted"
    REJECTED = "rejected"


class ReplayTestStatus(StrEnum):
    """Whether the dry run accepted the exact historical payload."""

    PASSED = "passed"
    FAILED = "failed"


class PlaybookStatus(StrEnum):
    """Whether a playbook is a current reference or retired history."""

    ACTIVE = "active"
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class ReplayValidationResult:
    """What one pure dry run concluded about one historical payload."""

    status: ReplayTestStatus
    issue_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        unknown = sorted(set(self.issue_codes) - REPLAY_ISSUE_CODES)
        if unknown:
            raise InvalidPlaybookReplayTest(
                "a replay validator may only report bounded issue codes, not "
                f"{unknown}"
            )
        if len(set(self.issue_codes)) != len(self.issue_codes):
            raise InvalidPlaybookReplayTest("a replay result must not repeat an issue code")
        if self.status is ReplayTestStatus.PASSED and self.issue_codes:
            raise InvalidPlaybookReplayTest("a passing replay result carries no issue codes")
        if self.status is ReplayTestStatus.FAILED and not self.issue_codes:
            raise InvalidPlaybookReplayTest("a failing replay result must say what went wrong")

    @property
    def passed(self) -> bool:
        """Whether the payload is still understood by the current code."""
        return self.status is ReplayTestStatus.PASSED

    @classmethod
    def pass_(cls) -> ReplayValidationResult:
        """The result of a payload the current parser accepts."""
        return cls(status=ReplayTestStatus.PASSED)

    @classmethod
    def fail(cls, *issue_codes: str) -> ReplayValidationResult:
        """The result of a payload the current parser refuses, with bounded codes."""
        return cls(status=ReplayTestStatus.FAILED, issue_codes=tuple(issue_codes))


@dataclass(frozen=True, slots=True)
class PlaybookCandidate:
    """A person's proposal to remember one successful action as a reviewed reference."""

    name: str
    note: str
    source_action_id: ActionRequestId
    source_execution_run_id: ExecutionRunId
    source_action_type: ActionType
    source_action_fingerprint: str
    created_at: datetime
    id: PlaybookCandidateId = field(default_factory=new_playbook_candidate_id)
    status: PlaybookCandidateStatus = PlaybookCandidateStatus.PENDING
    resolved_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", validate_candidate_name(self.name))
        object.__setattr__(self, "note", validate_candidate_note(self.note))
        if not isinstance(self.source_action_type, ActionType):
            object.__setattr__(
                self, "source_action_type", ActionType(self.source_action_type)
            )
        _require_fingerprint(
            self.source_action_fingerprint, "source_action_fingerprint", InvalidPlaybookCandidate
        )
        _require_aware(self.created_at, "created_at", InvalidPlaybookCandidate)
        _require_optional_aware(self.resolved_at, "resolved_at", InvalidPlaybookCandidate)
        resolved = self.status is not PlaybookCandidateStatus.PENDING
        if resolved and self.resolved_at is None:
            raise InvalidPlaybookCandidate("a resolved candidate must record resolved_at")
        if not resolved and self.resolved_at is not None:
            raise InvalidPlaybookCandidate("a pending candidate must not record resolved_at")

    @property
    def is_pending(self) -> bool:
        """Whether this candidate may still be tested, promoted or rejected."""
        return self.status is PlaybookCandidateStatus.PENDING

    def promote(self, at: datetime) -> PlaybookCandidate:
        """Move PENDING to PROMOTED.

        Raises:
            InvalidPlaybookCandidateTransition: this candidate is already resolved.
        """
        return self._resolve(at, PlaybookCandidateStatus.PROMOTED)

    def reject(self, at: datetime) -> PlaybookCandidate:
        """Move PENDING to REJECTED.

        Raises:
            InvalidPlaybookCandidateTransition: this candidate is already resolved.
        """
        return self._resolve(at, PlaybookCandidateStatus.REJECTED)

    def _resolve(self, at: datetime, target: PlaybookCandidateStatus) -> PlaybookCandidate:
        _require_aware(at, "at", InvalidPlaybookCandidate)
        if not self.is_pending:
            raise InvalidPlaybookCandidateTransition(self.id, self.status, target)
        return replace(self, status=target, resolved_at=at)


@dataclass(frozen=True, slots=True)
class PlaybookReplayTest:
    """One recorded dry run: the audit of "the current code still parses this payload"."""

    candidate_id: PlaybookCandidateId
    action_type: ActionType
    contract_version: int
    input_fingerprint: str
    status: ReplayTestStatus
    tested_at: datetime
    id: PlaybookReplayTestId = field(default_factory=new_playbook_replay_test_id)
    issue_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.action_type, ActionType):
            object.__setattr__(self, "action_type", ActionType(self.action_type))
        if not isinstance(self.contract_version, int) or isinstance(
            self.contract_version, bool
        ):
            raise InvalidPlaybookReplayTest("contract_version must be an integer")
        if self.contract_version < 1:
            raise InvalidPlaybookReplayTest("contract_version must be positive")
        _require_fingerprint(
            self.input_fingerprint, "input_fingerprint", InvalidPlaybookReplayTest
        )
        _require_aware(self.tested_at, "tested_at", InvalidPlaybookReplayTest)
        result = ReplayValidationResult(
            status=self.status, issue_codes=tuple(self.issue_codes)
        )
        object.__setattr__(self, "issue_codes", result.issue_codes)

    @property
    def passed(self) -> bool:
        """Whether this dry run accepted the payload."""
        return self.status is ReplayTestStatus.PASSED


@dataclass(frozen=True, slots=True)
class Playbook:
    """A reviewed, tested, non-executing reference blueprint."""

    candidate_id: PlaybookCandidateId
    name: str
    note: str
    action_type: ActionType
    source_action_id: ActionRequestId
    source_execution_run_id: ExecutionRunId
    source_action_fingerprint: str
    replay_contract_version: int
    promoted_from_test_id: PlaybookReplayTestId
    created_at: datetime
    id: PlaybookId = field(default_factory=new_playbook_id)
    status: PlaybookStatus = PlaybookStatus.ACTIVE
    retired_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", validate_candidate_name(self.name))
        object.__setattr__(self, "note", validate_candidate_note(self.note))
        if not isinstance(self.action_type, ActionType):
            object.__setattr__(self, "action_type", ActionType(self.action_type))
        _require_fingerprint(
            self.source_action_fingerprint, "source_action_fingerprint", InvalidPlaybook
        )
        if self.replay_contract_version < 1:
            raise InvalidPlaybook("a playbook records a positive replay contract version")
        _require_aware(self.created_at, "created_at", InvalidPlaybook)
        _require_optional_aware(self.retired_at, "retired_at", InvalidPlaybook)
        if self.status is PlaybookStatus.ACTIVE and self.retired_at is not None:
            raise InvalidPlaybook("an active playbook must not record retired_at")
        if self.status is PlaybookStatus.RETIRED and self.retired_at is None:
            raise InvalidPlaybook("a retired playbook must record retired_at")

    @property
    def is_active(self) -> bool:
        """Whether this playbook is still a current reference."""
        return self.status is PlaybookStatus.ACTIVE

    def retire(self, at: datetime) -> Playbook:
        """Move ACTIVE to RETIRED.

        Raises:
            InvalidPlaybookTransition: this playbook is already retired.
        """
        _require_aware(at, "at", InvalidPlaybook)
        if not self.is_active:
            raise InvalidPlaybookTransition(self.id, self.status, PlaybookStatus.RETIRED)
        return replace(self, status=PlaybookStatus.RETIRED, retired_at=at)


def replay_input_fingerprint(
    *,
    candidate_id: PlaybookCandidateId,
    source_action_id: ActionRequestId,
    source_action_fingerprint: str,
    source_action_type: ActionType,
    validator_action_type: ActionType,
    contract_version: int,
) -> str:
    """The SHA-256 that binds one dry run to one candidate snapshot and one contract version.

    It covers the identities and the versions, never the payload: an audit row has to be able to
    say *which* run was tested without becoming a second copy of a mail body or a form submission.
    """
    document = {
        "candidate_id": str(candidate_id),
        "contract_version": contract_version,
        "source_action_fingerprint": source_action_fingerprint,
        "source_action_id": str(source_action_id),
        "source_action_type": str(source_action_type),
        "validator_action_type": str(validator_action_type),
    }
    canonical = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "CANDIDATE_NAME_MAX_LENGTH",
    "CANDIDATE_NOTE_MAX_LENGTH",
    "FINGERPRINT_PATTERN",
    "ISSUE_ACTION_TYPE_MISMATCH",
    "ISSUE_PAYLOAD_INVALID",
    "ISSUE_SCHEMA_VERSION_UNSUPPORTED",
    "REPLAY_ISSUE_CODES",
    "Playbook",
    "PlaybookCandidate",
    "PlaybookCandidateId",
    "PlaybookCandidateStatus",
    "PlaybookId",
    "PlaybookReplayTest",
    "PlaybookReplayTestId",
    "PlaybookStatus",
    "ReplayTestStatus",
    "ReplayValidationResult",
    "new_playbook_candidate_id",
    "new_playbook_id",
    "new_playbook_replay_test_id",
    "replay_input_fingerprint",
    "validate_candidate_name",
    "validate_candidate_note",
]
