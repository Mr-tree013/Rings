"""Acceptance scenarios C and D: knowledge/learning, and the eHall safety chain (ADR-0031 §45/§46).

Scenario C walks the ground-truth path: index a local file, answer a question with an exact
citation, then let a *person* turn a correction into a confirmed fact — and prove that the confirmed
fact is still not injected into any eHall field, mail draft or model context.

Scenario D walks the certificate errand through both of its halves over the real pipeline policy: an
approved action whose page contract changed before execution fails *before* anything is typed or
clicked, and only a freshly prepared, freshly approved action against the matching page reaches the
single whitelisted submit.

Both use real SQLite, real stores, the real application services and the real pipeline policy; the
model, the page and the clock are the only scripted parts, and the socket guard stays on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assistant.adapters.content.registry import SuffixExtractorRegistry
from assistant.adapters.ehall.executor import EHallCertificateExecutor
from assistant.adapters.ehall.nju_certificate import RawField
from assistant.adapters.filesystem.scanner import FilesystemScanner
from assistant.adapters.filesystem.vault_manifest import VaultManifestFile
from assistant.adapters.knowledge.index_location import KnowledgeIndexLocator
from assistant.adapters.model.fake import FakeModelAdapter
from assistant.application.action_execution import ActionExecutionService
from assistant.application.approval_service import ApprovalService
from assistant.application.case_service import CaseService
from assistant.application.ehall_certificate import EHallCertificateService
from assistant.application.grounded_answer import GroundedAnswerService
from assistant.application.grounded_context import GroundedContextBuilder
from assistant.application.knowledge_indexer import KnowledgeIndexer
from assistant.application.knowledge_search import KnowledgeSearchService
from assistant.application.learning_service import LearningService
from assistant.application.mail_context import MailContextBuilder
from assistant.application.mail_drafts import MailDraftService
from assistant.application.storage_catalog import StorageCatalogService
from assistant.application.structured_model import StructuredModel
from assistant.domain.config import ModelConfig
from assistant.domain.ehall import EHallCertificatePayload
from assistant.domain.errors import ApprovalUnavailable, InvalidEHallForm
from assistant.domain.execution import ExecutionRunStatus
from assistant.domain.grounded_answer import GroundedAnswerStatus
from assistant.store.actions import SqliteActionRepository
from assistant.store.cases import SqliteCaseRepository
from assistant.store.catalog import SqliteCatalogRepository
from assistant.store.db import Database
from assistant.store.knowledge_index import SqliteKnowledgeIndexFactory
from assistant.store.learning import SqliteLearningRepository
from assistant.store.mail_drafts import SqliteMailDraftRepository
from assistant.store.migrations import apply_migrations
from tests.support.actions import SECRET_TOKEN, FixedTokenFactory
from tests.support.ehall import FIELD_KEY, SECOND_FIELD_KEY, FakeEHallPage, gateway_for
from tests.support.fakes import FakeClock
from tests.support.mail_drafts import draft_response
from tests.support.mail_intelligence import MailStores, build_message
from tests.support.ops import NOW

FACT_VALUE_SENTINEL = "FACT-SENTINEL-ROOM-302"
DOCUMENT_SENTINEL = "the registration deadline is October 23"


@pytest.fixture
def runtime_root(tmp_path: Path) -> Path:
    root = tmp_path / "data" / "growing-assistant"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=NOW)


@pytest.fixture
def database(runtime_root: Path, clock: FakeClock) -> Database:
    db = Database.at(runtime_root / "assistant.db")
    apply_migrations(db, clock=clock)
    return db


class _Knowledge:
    """The real indexing stack over one temporary local root."""

    def __init__(self, tmp_path: Path, database: Database, clock: FakeClock) -> None:
        self.locator = KnowledgeIndexLocator(cache_root=tmp_path / "cache" / "knowledge")
        self.catalog = SqliteCatalogRepository(database)
        self.manifests = VaultManifestFile(clock)
        self.indexes = SqliteKnowledgeIndexFactory(self.locator)
        self.indexer = KnowledgeIndexer(
            self.catalog, SuffixExtractorRegistry(), self.indexes, self.manifests, clock
        )
        self.search = KnowledgeSearchService(self.catalog, self.indexes, self.manifests)
        self.catalog_service = StorageCatalogService(
            FilesystemScanner(clock), self.manifests, self.catalog, clock
        )

    async def index(self, root: Path, name: str, text: str) -> None:
        root.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
        await self.catalog_service.scan_local(
            root_id="university", label="University", path=root
        )
        await self.indexer.index_root("university")


def _answered(text: str, source_ids: list[str]) -> str:
    return json.dumps(
        {
            "status": "answered",
            "segments": [{"text": text, "source_ids": source_ids}],
            "reason": None,
        }
    )


def _form_with_extra_option():
    """The same form with one more option: a contract change the user never approved."""
    from tests.support.ehall import standard_form

    return standard_form(
        fields=(
            RawField(key=FIELD_KEY, label="申请人姓名", kind="text", required=True),
            RawField(
                key=SECOND_FIELD_KEY,
                label="证明书类型",
                kind="select",
                required=True,
                options=("在读证明", "成绩证明", "毕业证明"),
            ),
        )
    )


# ------------------------------------------- Scenario C: knowledge, learning, no injection


async def test_scenario_c_grounded_answer_then_confirmed_fact_stays_out_of_other_contexts(
    database: Database, clock: FakeClock, tmp_path: Path
) -> None:
    """Index → cited answer → correction → confirmed fact, and the fact reaches nothing else."""
    knowledge = _Knowledge(tmp_path, database, clock)
    root = tmp_path / "documents" / "university"
    lines = [f"line {index}" for index in range(1, 20)] + [DOCUMENT_SENTINEL]
    await knowledge.index(root, "notes.md", "\n".join(lines))

    # Learn: a correction, a candidate, and the one human decision that creates a fact.
    learning = LearningService(SqliteLearningRepository(database), clock)
    proposal = await learning.propose_fact(
        "profile.office",
        FACT_VALUE_SENTINEL,
        f"My office is {FACT_VALUE_SENTINEL}, in the SE building.",
    )
    confirmation = await learning.confirm_fact(proposal.candidate.id)
    assert confirmation.fact.value == FACT_VALUE_SENTINEL
    assert [fact.value for fact in await learning.list_facts()] == [FACT_VALUE_SENTINEL]

    # Understand the document: the answer is grounded, and it cites the file it came from.
    answer_model = FakeModelAdapter().queue_text(
        _answered("the registration deadline is October 23", ["S1"])
    )
    answerer = GroundedAnswerService(
        GroundedContextBuilder(knowledge.search), StructuredModel(answer_model), ModelConfig()
    )
    result = await answerer.answer("What is the registration deadline?")

    assert result.answer.status is GroundedAnswerStatus.ANSWERED
    (evidence,) = result.cited_evidence()
    assert str(evidence.logical_uri) == "local://university/notes.md"
    assert evidence.source_span.line_start is not None
    assert evidence.source_span.line_start <= 20 <= (evidence.source_span.line_end or 0)
    assert DOCUMENT_SENTINEL in evidence.content
    # A confirmed fact is not knowledge: answering a question does not consult it.
    assert len(answer_model.requests) == 1
    assert FACT_VALUE_SENTINEL not in answer_model.requests[0].messages[0].content

    # Mail drafting still uses no personal facts at all, even with one confirmed.
    stores = MailStores(database, clock)
    message = await stores.store(
        build_message(
            message_id_header="<scenario-c@example.edu>",
            subject="About the deadline",
            body_text="Which day is the report due?",
        )
    )
    draft_model = FakeModelAdapter().queue_text(draft_response())
    drafts = MailDraftService(
        stores.mail,
        stores.intelligence,
        SqliteMailDraftRepository(database),
        MailContextBuilder(stores.mail, stores.intelligence),
        None,
        StructuredModel(draft_model),
        clock,
    )
    await drafts.create_reply_draft(message.id)
    assert len(draft_model.requests) == 1  # the check below is only meaningful if it ran
    for request in draft_model.requests:
        assert FACT_VALUE_SENTINEL not in request.messages[0].content

    # And a confirmed fact never fills an eHall field: prepare takes explicit values only.
    actions = SqliteActionRepository(database)
    cases = SqliteCaseRepository(database)
    case = await CaseService(cases, actions, clock).create_case("Certificate application")
    with pytest.raises(InvalidEHallForm):  # a required field is still required, fact or not
        await EHallCertificateService(
            gateway_for(FakeEHallPage()), cases, actions, clock
        ).prepare(case_id=case.id, field_values={})
    preparation = await EHallCertificateService(
        gateway_for(FakeEHallPage()), cases, actions, clock
    ).prepare(
        case_id=case.id,
        field_values={FIELD_KEY: "张同学", SECOND_FIELD_KEY: "在读证明"},
    )
    payload = EHallCertificatePayload.from_payload(preparation.action.payload)
    assert [(field.key, field.value) for field in payload.fields] == [
        (FIELD_KEY, "张同学"),
        (SECOND_FIELD_KEY, "在读证明"),
    ]
    assert FACT_VALUE_SENTINEL not in json.dumps(preparation.action.payload)


# ------------------------------------------------ Scenario D: the certificate safety chain


async def test_scenario_d_a_changed_page_submits_nothing_and_the_matching_page_submits_once(
    database: Database, clock: FakeClock
) -> None:
    """Approve contract A, meet contract B → FAILED and no submit; then approve B → one submit."""
    actions = SqliteActionRepository(database)
    cases = CaseService(SqliteCaseRepository(database), actions, clock)
    approvals = ApprovalService(actions, clock, token_factory=FixedTokenFactory(SECRET_TOKEN))
    values = {FIELD_KEY: "张同学", SECOND_FIELD_KEY: "在读证明"}
    changed_form = _form_with_extra_option()

    first_case = await cases.create_case("Certificate prepared against the old page")
    first = await EHallCertificateService(
        gateway_for(FakeEHallPage()), SqliteCaseRepository(database), actions, clock
    ).prepare(case_id=first_case.id, field_values=values)
    issued = await approvals.create_challenge(first.action.id)
    await approvals.approve(first.action.id, issued.token)

    changed_page = FakeEHallPage(form=changed_form)
    executor = EHallCertificateExecutor(gateway_for(changed_page))
    execution = ActionExecutionService(actions, {executor.action_type: executor}, clock)
    failed = await execution.execute(first.action.id)

    # The contract was re-checked before anything was typed, and nothing was submitted.
    assert failed.run.status is ExecutionRunStatus.FAILED
    assert failed.run.status.value == "failed"
    assert changed_page.fills == []
    assert changed_page.clicks == []
    # The attempt consumed the approval: running it again needs a new human decision, not a retry.
    with pytest.raises(ApprovalUnavailable):
        await execution.execute(first.action.id)

    # A fresh action against the page as it now is: one approval, one fill, one submit.
    matching_page = FakeEHallPage(form=changed_form)
    second_case = await cases.create_case("Certificate prepared against the current page")
    second = await EHallCertificateService(
        gateway_for(FakeEHallPage(form=changed_form)),
        SqliteCaseRepository(database),
        actions,
        clock,
    ).prepare(case_id=second_case.id, field_values=values)
    second_issued = await approvals.create_challenge(second.action.id)
    await approvals.approve(second.action.id, second_issued.token)
    second_executor = EHallCertificateExecutor(gateway_for(matching_page))
    succeeded = await ActionExecutionService(
        actions, {second_executor.action_type: second_executor}, clock
    ).execute(second.action.id)

    assert succeeded.run.status is ExecutionRunStatus.SUCCEEDED
    assert matching_page.clicks == ["certificate-submit"]
    assert list(matching_page.fills) == [
        (FIELD_KEY, "张同学"),
        (SECOND_FIELD_KEY, "在读证明"),
    ]
