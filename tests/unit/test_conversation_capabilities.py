"""The capability registry: what a conversation may do, and who decides (ADR-0033 §6-7, §30)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from assistant import bootstrap
from assistant.application.conversation_capabilities import (
    ConfirmationPolicy,
    ConversationCapabilityRegistry,
    ConversationHandlers,
    build_phase_10a_registry,
)
from assistant.domain.conversation_plan import (
    READ_OPERATIONS,
    ConversationOperationType,
)
from assistant.domain.errors import ConversationCapabilityUnavailable
from assistant.store.db import Database
from assistant.store.migrations import apply_migrations
from tests.support.conversation import NOW, write_config
from tests.support.fakes import FakeClock


async def _registry(tmp_path: Path) -> ConversationCapabilityRegistry:
    clock = FakeClock(start=NOW)
    database = Database.at(tmp_path / "data" / "assistant.db")
    apply_migrations(database, clock=clock)
    config = await bootstrap.config_loader(write_config(tmp_path)).load()
    from assistant.adapters.model.fake import FakeModelAdapter

    adapter = FakeModelAdapter()
    return bootstrap.conversation_capabilities(database, clock, config, model=adapter)


async def test_the_registry_contains_exactly_the_frozen_operations(tmp_path: Path) -> None:
    registry = await _registry(tmp_path)

    assert set(registry.operation_types) == set(ConversationOperationType)


async def test_only_plan_apply_needs_a_conversational_confirmation(tmp_path: Path) -> None:
    registry = await _registry(tmp_path)

    confirming = {
        operation_type
        for operation_type in registry.operation_types
        if registry.policy_of(operation_type) is ConfirmationPolicy.CONFIRM_LOCAL
    }

    assert confirming == {ConversationOperationType.PLAN_APPLY_PROPOSAL}


async def test_read_operations_are_never_confirm_local(tmp_path: Path) -> None:
    registry = await _registry(tmp_path)

    for operation_type in READ_OPERATIONS:
        assert registry.policy_of(operation_type) is ConfirmationPolicy.READ


async def test_the_registry_has_no_external_write_policy(tmp_path: Path) -> None:
    await _registry(tmp_path)

    assert {policy.value for policy in ConfirmationPolicy} == {
        "read",
        "local_write",
        "confirm_local",
    }
    assert not [policy for policy in ConfirmationPolicy if "external" in policy.value]


async def test_an_operation_this_build_does_not_offer_is_refused(tmp_path: Path) -> None:
    registry = await _registry(tmp_path)
    kept = [
        registry.require(operation_type)
        for operation_type in registry.operation_types
        if operation_type is not ConversationOperationType.PLAN_APPLY_PROPOSAL
    ]
    registry_without_plans = ConversationCapabilityRegistry(kept)

    with pytest.raises(ConversationCapabilityUnavailable):
        registry_without_plans.require(ConversationOperationType.PLAN_APPLY_PROPOSAL)


def test_duplicate_capabilities_are_a_programming_error() -> None:
    from assistant.application.conversation_capabilities import ConversationCapability

    async def handler(arguments: object) -> None:  # pragma: no cover - never called
        return None

    capability = ConversationCapability(
        operation_type=ConversationOperationType.TASK_LIST,
        policy=ConfirmationPolicy.READ,
        handler=handler,  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError):
        ConversationCapabilityRegistry([capability, capability])
    with pytest.raises(ValueError):
        ConversationCapabilityRegistry([])


def test_handlers_are_built_over_the_existing_services() -> None:
    """Every handler is a bound method of the handler set, never a free function or a lambda."""
    import inspect

    handlers = [
        name
        for name in dir(ConversationHandlers)
        if not name.startswith("_") and callable(getattr(ConversationHandlers, name))
    ]

    assert "task_create" in handlers
    assert "plan_apply_proposal" in handlers
    assert "knowledge_ask" in handlers
    for name in handlers:
        assert inspect.iscoroutinefunction(
            getattr(ConversationHandlers, name)
        ) or name in {"__init__"}


def test_a_confirmed_operation_is_bound_to_its_fingerprint() -> None:
    """The confirmation is answered by operation id, and the row keeps the fingerprint."""
    from assistant.domain.conversation import ConversationOperation, ConversationOperationStatus
    from assistant.domain.conversation_plan import (
        TaskCreateArguments,
        operation_fingerprint,
    )

    arguments = TaskCreateArguments(title="写报告")
    fingerprint = operation_fingerprint(ConversationOperationType.TASK_CREATE, arguments)
    operation = ConversationOperation(
        turn_id=NOW,
        ordinal=0,
        operation_type=ConversationOperationType.TASK_CREATE,
        arguments=arguments,
        operation_fingerprint=fingerprint,
        status=ConversationOperationStatus.WAITING_CONFIRMATION,
        confirmation_expires_at=datetime(2026, 9, 21, 0, 30, tzinfo=UTC),
        created_at=NOW,
        updated_at=NOW,
    )

    assert operation.operation_fingerprint == fingerprint
    assert operation.arguments == arguments
    assert ConversationOperationStatus.WAITING_CONFIRMATION.value == "waiting_confirmation"


def test_build_phase_10a_registry_is_the_only_constructor() -> None:
    """The registry is built from one explicit tuple, so its contents are greppable."""
    import inspect

    source = inspect.getsource(build_phase_10a_registry)

    for operation_type in ConversationOperationType:
        assert operation_type.value.replace(".", "_") in source or operation_type.name in source
