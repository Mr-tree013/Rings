"""The playbook service end to end (ADR-0028).

Real SQLite, real actions, real execution runs, real replay validators. What is under test is the
whole promise of the phase: a *definitive success* can become a reviewed, tested reference — and
nothing else can. Successful runs do not become playbooks by themselves, a dry run performs no
side effect, and promotion needs a pass that still applies.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from assistant.adapters.ehall.executor import EHallCertificateExecutor
from assistant.adapters.mail.smtp import SmtpMailExecutor
from assistant.application.playbook_replay import (
    PlaybookReplayRegistry,
)
from assistant.domain.errors import (
    ActionRequestNotFound,
    InvalidPlaybookCandidateTransition,
    PlaybookCandidateExists,
    PlaybookCandidateNotTested,
    PlaybookReplayUnsupported,
    PlaybookSourceIntegrityError,
    PlaybookSourceNotEligible,
    PlaybookSourceUnsupported,
)
from assistant.domain.playbook import (
    ISSUE_PAYLOAD_INVALID,
    ISSUE_SCHEMA_VERSION_UNSUPPORTED,
    PlaybookCandidateStatus,
    PlaybookStatus,
    ReplayTestStatus,
)
from assistant.ports.ehall_certificate import EHallSubmissionOutcome
from assistant.store.db import Database
from assistant.store.migrations import apply_migrations
from tests.support.actions import SECRET_TOKEN
from tests.support.ehall import FakeCertificateGateway, standard_form
from tests.support.fakes import FakeClock
from tests.support.mail_send import (
    SEND_ACCOUNT_ID,
    SMTP_PASSWORD,
    StrictSmtpServer,
    smtp_account,
)
from tests.support.playbooks import (
    EHALL_ACTION_TYPE,
    FUTURE_ACTION_TYPE,
    MAIL_SEND_ACTION_TYPE,
    MESSAGE_ID,
    NOW,
    PlaybookHarness,
    RecordingReplayValidator,
    default_registry,
    ehall_payload,
    mail_send_payload,
)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=NOW)


@pytest.fixture
def database(tmp_path: Path, clock: FakeClock) -> Database:
    db = Database.at(tmp_path / "assistant.db")
    apply_migrations(db, clock=clock)
    return db


@pytest.fixture
def harness(database: Database, clock: FakeClock) -> PlaybookHarness:
    return PlaybookHarness(database, clock)


# ------------------------------------------------------------------ source eligibility


async def test_a_definitive_success_can_seed_a_candidate(
    harness: PlaybookHarness,
) -> None:
    action, run, _ = await harness.executed()

    candidate = await harness.service().create_candidate(
        str(action.id)[:8], name="Approved mail shape", note="Reviewed the successful run."
    )

    assert candidate.source_action_id == action.id
    assert candidate.source_execution_run_id == run.id
    assert candidate.source_action_type == MAIL_SEND_ACTION_TYPE
    assert candidate.source_action_fingerprint == action.fingerprint
    assert candidate.status is PlaybookCandidateStatus.PENDING
    assert candidate.is_pending is True


async def test_a_successful_execution_never_creates_a_candidate_by_itself(
    harness: PlaybookHarness,
) -> None:
    """§33: running the action is not learning from it; only a person turns a run into a lesson."""
    await harness.executed()

    assert await harness.playbooks.list_candidates(limit=None) == []
    assert await harness.service().list_candidates(limit=None) == []


async def test_an_action_that_never_ran_cannot_seed_a_candidate(
    harness: PlaybookHarness,
) -> None:
    prepared = await harness.prepare()

    with pytest.raises(PlaybookSourceNotEligible) as raised:
        await harness.service().create_candidate(
            prepared.id, name="x", note="y"
        )

    assert "not executed" in raised.value.reason
    assert await harness.playbooks.list_candidates(limit=None) == []


@pytest.mark.parametrize("script", ["failure", "unknown"])
async def test_a_run_that_did_not_succeed_cannot_seed_a_candidate(
    harness: PlaybookHarness, script: str
) -> None:
    from assistant.application.action_execution import ActionExecutionService
    from assistant.application.approval_service import ApprovalService
    from tests.support.actions import FakeActionExecutor

    action = await harness.prepare()
    approvals = ApprovalService(harness.actions, harness.clock, token_factory=harness.tokens)
    issued = await approvals.create_challenge(action.id)
    await approvals.approve(action.id, issued.token)
    executor = FakeActionExecutor(action_type=MAIL_SEND_ACTION_TYPE)
    if script == "failure":
        executor.script_failure()
    else:
        executor.script_unknown()
    await ActionExecutionService(
        harness.actions, {MAIL_SEND_ACTION_TYPE: executor}, harness.clock
    ).execute(action.id)

    with pytest.raises(PlaybookSourceNotEligible):
        await harness.service().create_candidate(action.id, name="x", note="y")


async def test_a_crashed_run_leaves_nothing_to_learn_from(
    harness: PlaybookHarness,
) -> None:
    """A `RUNNING` run is a question, not an answer: it can never seed a candidate."""
    from assistant.application.action_execution import ActionExecutionService
    from assistant.application.approval_service import ApprovalService
    from tests.support.actions import FakeActionExecutor

    action = await harness.prepare()
    approvals = ApprovalService(harness.actions, harness.clock, token_factory=harness.tokens)
    issued = await approvals.create_challenge(action.id)
    await approvals.approve(action.id, issued.token)
    executor = FakeActionExecutor(action_type=MAIL_SEND_ACTION_TYPE).script_cancel()
    service = ActionExecutionService(
        harness.actions, {MAIL_SEND_ACTION_TYPE: executor}, harness.clock
    )
    running = asyncio.create_task(service.execute(action.id))
    await asyncio.wait_for(executor.started.wait(), timeout=5)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    with pytest.raises(PlaybookSourceNotEligible):
        await harness.service().create_candidate(action.id, name="x", note="y")


async def test_an_executed_action_whose_run_is_not_a_success_is_refused(
    harness: PlaybookHarness, database: Database
) -> None:
    action, run, _ = await harness.executed()
    with database.connect() as connection:
        connection.execute(
            "UPDATE execution_runs SET status = 'unknown' WHERE id = ?", (str(run.id),)
        )

    with pytest.raises(PlaybookSourceNotEligible):
        await harness.service().create_candidate(action.id, name="x", note="y")


async def test_an_action_type_with_no_validator_cannot_seed_a_candidate(
    harness: PlaybookHarness,
) -> None:
    """§38: refusing at creation means every pending candidate has a test it could pass."""
    action, _, _ = await harness.executed(
        FUTURE_ACTION_TYPE, {"whatever": "this capability does not exist"}
    )

    with pytest.raises(PlaybookSourceUnsupported):
        await harness.service().create_candidate(action.id, name="x", note="y")

    assert await harness.playbooks.list_candidates(limit=None) == []


async def test_one_action_seeds_one_candidate_only(harness: PlaybookHarness) -> None:
    action, _, _ = await harness.executed()
    service = harness.service()
    await service.create_candidate(action.id, name="First", note="Reviewed.")

    with pytest.raises(PlaybookCandidateExists):
        await service.create_candidate(action.id, name="Second", note="Reviewed again.")


async def test_an_unknown_action_reference_is_refused(harness: PlaybookHarness) -> None:
    with pytest.raises(ActionRequestNotFound):
        await harness.service().create_candidate("ffffffff", name="x", note="y")


# ------------------------------------------------------------------------- dry runs


async def test_a_dry_run_records_a_pass_for_the_exact_payload(
    harness: PlaybookHarness,
) -> None:
    action, _, executor = await harness.executed()
    service = harness.service()
    candidate = await service.create_candidate(action.id, name="Approved", note="Reviewed.")

    test = await service.test_candidate(candidate.id)

    assert test.status is ReplayTestStatus.PASSED
    assert test.issue_codes == ()
    assert test.contract_version == 1
    assert test.action_type == MAIL_SEND_ACTION_TYPE
    assert len(test.input_fingerprint) == 64
    # The source execution happened exactly once, and the dry run did not add a second call.
    assert len(executor.calls) == 1
    assert (await harness.actions.count_executions(action.id)) == 1


async def test_the_dry_run_hands_the_validator_the_exact_stored_action(
    harness: PlaybookHarness,
) -> None:
    action, _, _ = await harness.executed()
    validator = RecordingReplayValidator()
    service = harness.service(replay=default_registry(validator))
    candidate = await service.create_candidate(action.id, name="Approved", note="Reviewed.")

    await service.test_candidate(candidate.id)

    assert len(validator.calls) == 1
    assert validator.calls[0].id == action.id
    assert validator.calls[0].payload_json == action.payload_json


async def test_a_payload_the_current_parser_refuses_fails_the_dry_run(
    harness: PlaybookHarness,
) -> None:
    """A stored payload can be well-fingered and still be something today's code will not accept."""
    action, _, _ = await harness.executed(
        MAIL_SEND_ACTION_TYPE, {"to": "ada@example.edu", "subject": "hello"}
    )
    service = harness.service()
    candidate = await service.create_candidate(action.id, name="Old shape", note="Historical.")

    test = await service.test_candidate(candidate.id)

    assert test.status is ReplayTestStatus.FAILED
    assert test.issue_codes == (ISSUE_PAYLOAD_INVALID,)


async def test_an_unsupported_schema_version_fails_with_its_own_code(
    harness: PlaybookHarness,
) -> None:
    payload = mail_send_payload()
    payload["schema_version"] = 99
    action, _, _ = await harness.executed(MAIL_SEND_ACTION_TYPE, payload)
    service = harness.service()
    candidate = await service.create_candidate(action.id, name="Future", note="Historical.")

    test = await service.test_candidate(candidate.id)

    assert test.issue_codes == (ISSUE_SCHEMA_VERSION_UNSUPPORTED,)


async def test_a_tampered_payload_is_an_integrity_failure_not_a_failed_dry_run(
    harness: PlaybookHarness, database: Database
) -> None:
    """§37: corruption raises, and nothing is recorded as if it were an ordinary result.

    A stored action whose payload no longer matches its fingerprint is refused by the Phase 6A
    repository *before* this service sees it, which is the strongest version of the rule: the
    tampered row is not even loadable.
    """
    from assistant.store.errors import CommitmentStoreError

    action, _, _ = await harness.executed()
    service = harness.service()
    candidate = await service.create_candidate(action.id, name="Approved", note="Reviewed.")
    with database.connect() as connection:
        connection.execute(
            "UPDATE action_requests SET payload_json = ? WHERE id = ?",
            ('{"to":"attacker@example.com"}', str(action.id)),
        )

    with pytest.raises(CommitmentStoreError):
        await service.test_candidate(candidate.id)
    with pytest.raises(CommitmentStoreError):
        await service.promote_candidate(candidate.id)

    assert await harness.playbooks.list_replay_tests(candidate.id) == []
    assert await harness.playbooks.list_playbooks(limit=None) == []
    still_pending = await harness.playbooks.get_candidate(candidate.id)
    assert still_pending is not None and still_pending.is_pending


async def test_a_candidate_whose_recorded_fingerprint_drifted_is_an_integrity_failure(
    harness: PlaybookHarness, database: Database
) -> None:
    """The snapshot the reviewer read must still be the action's fingerprint at review time."""
    action, _, _ = await harness.executed()
    service = harness.service()
    candidate = await service.create_candidate(action.id, name="Approved", note="Reviewed.")
    with database.connect() as connection:
        connection.execute(
            "UPDATE playbook_candidates SET source_action_fingerprint = ? WHERE id = ?",
            ("f" * 64, str(candidate.id)),
        )

    with pytest.raises(PlaybookSourceIntegrityError):
        await service.test_candidate(candidate.id)
    with pytest.raises(PlaybookSourceIntegrityError):
        await service.promote_candidate(candidate.id)

    assert await harness.playbooks.list_replay_tests(candidate.id) == []


async def test_a_missing_source_action_is_an_integrity_failure(
    harness: PlaybookHarness, database: Database
) -> None:
    action, _, _ = await harness.executed()
    service = harness.service()
    candidate = await service.create_candidate(action.id, name="Approved", note="Reviewed.")
    with database.connect() as connection:
        connection.execute("PRAGMA foreign_keys = OFF")  # simulate a corrupted store
        connection.execute("DELETE FROM execution_runs WHERE action_id = ?", (str(action.id),))

    with pytest.raises(PlaybookSourceIntegrityError):
        await service.test_candidate(candidate.id)


async def test_testing_a_resolved_candidate_is_refused(harness: PlaybookHarness) -> None:
    action, _, _ = await harness.executed()
    service = harness.service()
    candidate = await service.create_candidate(action.id, name="Approved", note="Reviewed.")
    await service.test_candidate(candidate.id)
    await service.promote_candidate(candidate.id)

    with pytest.raises(InvalidPlaybookCandidateTransition):
        await service.test_candidate(candidate.id)


async def test_two_dry_runs_are_two_audit_rows(harness: PlaybookHarness) -> None:
    """§43: replay is a read-only validation, so running it twice appends twice."""
    action, _, _ = await harness.executed()
    service = harness.service()
    candidate = await service.create_candidate(action.id, name="Approved", note="Reviewed.")

    await service.test_candidate(candidate.id)
    harness.clock.advance(60)
    await service.test_candidate(candidate.id)

    detail = await service.get_candidate(candidate.id)
    assert [item.status for item in detail.tests] == [
        ReplayTestStatus.PASSED,
        ReplayTestStatus.PASSED,
    ]


async def test_an_unknown_replay_capability_is_refused_at_test_time(
    harness: PlaybookHarness,
) -> None:
    """A validator that disappears between creation and review must not quietly pass."""
    action, _, _ = await harness.executed()
    candidate = await harness.service().create_candidate(
        action.id, name="Approved", note="Reviewed."
    )

    with pytest.raises(PlaybookReplayUnsupported):
        await harness.service(replay=PlaybookReplayRegistry()).test_candidate(candidate.id)


# ------------------------------------------------------------------------ promotion


async def test_promotion_requires_a_current_passing_dry_run(
    harness: PlaybookHarness,
) -> None:
    action, _, _ = await harness.executed()
    service = harness.service()
    candidate = await service.create_candidate(action.id, name="Approved", note="Reviewed.")

    with pytest.raises(PlaybookCandidateNotTested):
        await service.promote_candidate(candidate.id)


async def test_a_pass_from_an_older_contract_version_does_not_qualify(
    harness: PlaybookHarness,
) -> None:
    action, _, _ = await harness.executed()
    candidate = await harness.service(
        replay=default_registry(RecordingReplayValidator(contract_version=1))
    ).create_candidate(action.id, name="Approved", note="Reviewed.")
    await harness.service(
        replay=default_registry(RecordingReplayValidator(contract_version=1))
    ).test_candidate(candidate.id)

    with pytest.raises(PlaybookCandidateNotTested):
        await harness.service(
            replay=default_registry(RecordingReplayValidator(contract_version=2))
        ).promote_candidate(candidate.id)

    # Re-testing under the new contract is what makes it promotable.
    service = harness.service(
        replay=default_registry(RecordingReplayValidator(contract_version=2))
    )
    await service.test_candidate(candidate.id)
    playbook = await service.promote_candidate(candidate.id)

    assert playbook.replay_contract_version == 2


async def test_a_promoted_playbook_keeps_its_provenance_and_test(
    harness: PlaybookHarness,
) -> None:
    action, run, _ = await harness.executed()
    service = harness.service()
    candidate = await service.create_candidate(action.id, name="Approved", note="Reviewed.")
    test = await service.test_candidate(candidate.id)

    playbook = await service.promote_candidate(candidate.id)

    assert playbook.name == candidate.name
    assert playbook.note == candidate.note
    assert playbook.action_type == MAIL_SEND_ACTION_TYPE
    assert playbook.source_action_id == action.id
    assert playbook.source_execution_run_id == run.id
    assert playbook.source_action_fingerprint == action.fingerprint
    assert playbook.promoted_from_test_id == test.id
    assert playbook.status is PlaybookStatus.ACTIVE
    detail = await service.get_playbook(playbook.id)
    assert detail.candidate.id == candidate.id
    assert detail.promotion_test is not None
    assert detail.promotion_test.id == test.id


async def test_a_rejected_candidate_can_never_be_promoted(
    harness: PlaybookHarness,
) -> None:
    action, _, _ = await harness.executed()
    service = harness.service()
    candidate = await service.create_candidate(action.id, name="Approved", note="Reviewed.")
    await service.test_candidate(candidate.id)

    rejected = await service.reject_candidate(candidate.id)

    assert rejected.status is PlaybookCandidateStatus.REJECTED
    with pytest.raises(InvalidPlaybookCandidateTransition):
        await service.promote_candidate(candidate.id)
    assert await harness.playbooks.list_playbooks(limit=None) == []


async def test_retiring_keeps_the_playbook_and_its_test_history(
    harness: PlaybookHarness,
) -> None:
    action, _, _ = await harness.executed()
    service = harness.service()
    candidate = await service.create_candidate(action.id, name="Approved", note="Reviewed.")
    await service.test_candidate(candidate.id)
    playbook = await service.promote_candidate(candidate.id)

    retired = await service.retire_playbook(playbook.id)

    assert retired.status is PlaybookStatus.RETIRED
    assert await service.list_playbooks(statuses=(PlaybookStatus.ACTIVE,), limit=None) == []
    history = await service.list_playbooks(limit=None)
    assert [item.id for item in history] == [playbook.id]
    detail = await service.get_playbook(playbook.id)
    assert detail.playbook.status is PlaybookStatus.RETIRED
    assert detail.promotion_test is not None


# ------------------------------------------------------------------ real capabilities


async def test_the_certificate_capability_reviews_without_touching_the_browser(
    harness: PlaybookHarness,
) -> None:
    """The strongest form of §35: a real executor whose gateway counts every submission."""
    action = await harness.prepare(EHALL_ACTION_TYPE, ehall_payload())
    gateway = FakeCertificateGateway(
        snapshot=standard_form(), outcome=EHallSubmissionOutcome.SUCCEEDED
    )
    from assistant.application.action_execution import ActionExecutionService
    from assistant.application.approval_service import ApprovalService

    approvals = ApprovalService(harness.actions, harness.clock, token_factory=harness.tokens)
    issued = await approvals.create_challenge(action.id)
    await approvals.approve(action.id, issued.token)
    await ActionExecutionService(
        harness.actions,
        {EHALL_ACTION_TYPE: EHallCertificateExecutor(gateway)},
        harness.clock,
    ).execute(action.id)
    assert gateway.submit_count == 1  # the one real submission this phase is allowed to see
    assert await harness.playbooks.list_candidates(limit=None) == []

    service = harness.service()
    candidate = await service.create_candidate(action.id, name="Certificate", note="Reviewed.")
    test = await service.test_candidate(candidate.id)
    playbook = await service.promote_candidate(candidate.id)

    assert test.status is ReplayTestStatus.PASSED
    assert test.contract_version == 1
    assert playbook.action_type == EHALL_ACTION_TYPE
    # The dry run and the promotion opened no browser and submitted nothing.
    assert gateway.submit_count == 1
    assert gateway.inspections == 0


async def test_a_real_smtp_executor_sends_once_and_the_review_sends_nothing(
    harness: PlaybookHarness,
) -> None:
    action = await harness.prepare(MAIL_SEND_ACTION_TYPE, mail_send_payload())
    server = StrictSmtpServer()
    executor = SmtpMailExecutor(
        {SEND_ACCOUNT_ID: smtp_account()},
        password_lookup=lambda account_id: SMTP_PASSWORD,
        client_factory=lambda target: server,
    )
    from assistant.application.action_execution import ActionExecutionService
    from assistant.application.approval_service import ApprovalService

    approvals = ApprovalService(harness.actions, harness.clock, token_factory=harness.tokens)
    issued = await approvals.create_challenge(action.id)
    await approvals.approve(action.id, issued.token)
    await ActionExecutionService(
        harness.actions, {MAIL_SEND_ACTION_TYPE: executor}, harness.clock
    ).execute(action.id)

    assert server.stages.count("data") == 1
    assert server.sent_bytes is not None
    assert MESSAGE_ID.encode() in server.sent_bytes

    service = harness.service()
    candidate = await service.create_candidate(action.id, name="Mail shape", note="Reviewed.")
    await service.test_candidate(candidate.id)
    await service.promote_candidate(candidate.id)

    # Not one extra SMTP stage, and no credential was needed for the dry run either.
    assert server.stages.count("data") == 1
    assert server.stages.count("login") == 1


async def test_a_dry_run_does_not_need_a_credential(
    harness: PlaybookHarness,
) -> None:
    """§36: the pass is about a payload, not about this machine's configuration."""
    action = await harness.prepare(MAIL_SEND_ACTION_TYPE, mail_send_payload())
    server = StrictSmtpServer()
    executor = SmtpMailExecutor(
        {SEND_ACCOUNT_ID: smtp_account()},
        password_lookup=lambda account_id: SMTP_PASSWORD,
        client_factory=lambda target: server,
    )
    from assistant.application.action_execution import ActionExecutionService
    from assistant.application.approval_service import ApprovalService

    approvals = ApprovalService(harness.actions, harness.clock, token_factory=harness.tokens)
    issued = await approvals.create_challenge(action.id)
    await approvals.approve(action.id, issued.token)
    await ActionExecutionService(
        harness.actions, {MAIL_SEND_ACTION_TYPE: executor}, harness.clock
    ).execute(action.id)
    # The credential is gone from this point on.
    unconfigured = SmtpMailExecutor(
        {SEND_ACCOUNT_ID: smtp_account()},
        password_lookup=lambda account_id: None,
        client_factory=lambda target: server,
    )

    service = harness.service()
    candidate = await service.create_candidate(action.id, name="Mail shape", note="Reviewed.")
    test = await service.test_candidate(candidate.id)
    playbook = await service.promote_candidate(candidate.id)

    assert unconfigured.supports(action) is False  # it really could not send today
    assert test.status is ReplayTestStatus.PASSED
    assert playbook.replay_contract_version == 1
    assert server.stages.count("data") == 1


# ------------------------------------------------------------- no-mutation guarantee


async def test_the_whole_flow_touches_only_the_playbook_tables(
    harness: PlaybookHarness, database: Database
) -> None:
    """§49: reviewing a success must not rewrite actions, approvals or anything else."""
    action, _, _ = await harness.executed()
    before = await _counts(database)
    service = harness.service()

    candidate = await service.create_candidate(action.id, name="Approved", note="Reviewed.")
    await service.test_candidate(candidate.id)
    playbook = await service.promote_candidate(candidate.id)
    await service.retire_playbook(playbook.id)

    after = await _counts(database)
    changed = {table for table in before if before[table] != after[table]}

    assert changed == {
        "playbook_candidates",
        "playbook_replay_tests",
        "playbooks",
    }
    assert after["playbook_candidates"] == 1
    assert after["playbook_replay_tests"] == 1
    assert after["playbooks"] == 1
    assert after["action_requests"] == before["action_requests"]
    assert after["approvals"] == before["approvals"]
    assert after["execution_runs"] == before["execution_runs"]


async def test_reviewing_logs_neither_the_payload_nor_the_note(
    harness: PlaybookHarness, caplog: pytest.LogCaptureFixture
) -> None:
    """A dry run records identities, never the body of the thing being validated."""
    secret_body = "Dear Ada, the account number is 1234567890."
    action, _, _ = await harness.executed(
        MAIL_SEND_ACTION_TYPE, mail_send_payload(body_text=secret_body)
    )
    service = harness.service()

    with caplog.at_level("DEBUG"):
        candidate = await service.create_candidate(
            action.id, name="A memorable run", note="The note with 1234567890 in it."
        )
        await service.test_candidate(candidate.id)
        await service.promote_candidate(candidate.id)

    assert secret_body not in caplog.text
    assert "1234567890" not in caplog.text
    assert SECRET_TOKEN not in caplog.text


async def _counts(database: Database) -> dict[str, int]:
    with database.connect() as connection:
        tables = [
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        ]
        counted: dict[str, int] = {}
        for table in sorted(tables):
            row = connection.execute(f"SELECT count(*) AS total FROM {table}").fetchone()
            counted[table] = int(row["total"])
    return counted


async def test_the_service_never_creates_an_action_or_an_approval(
    harness: PlaybookHarness,
) -> None:
    """The executor set is irrelevant to this service: it is not asked for one."""
    action, _, _ = await harness.executed()
    service = harness.service()
    candidate = await service.create_candidate(action.id, name="Approved", note="Reviewed.")
    await service.test_candidate(candidate.id)
    playbook = await service.promote_candidate(candidate.id)

    assert await harness.actions.count_actions() == 1
    assert len(await harness.actions.list_executions(action.id)) == 1
    assert await harness.actions.get_action(playbook.source_action_id) is not None
    approvals = await harness.actions.list_approvals(action.id)
    assert len(approvals) == 1
    assert approvals[0].consumed_at is not None  # the original approval, long since spent
