"""The local MCP surface, in process: exact discovery, bounded reads, gated writes (ADR-0030).

Real SQLite, the real facade, the real server, driven by the SDK's own client over an in-memory
transport. What is under test is what a client can discover and what it can do: the exact resource
and tool lists for each capability mode, the guarantee that a capability which is not configured
does not exist, typed errors instead of internals, and — the point of the whole phase — that no
amount of calling anything reaches an approval, an execution, a mail body, a fact, a playbook or a
network.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
from mcp import Client

from assistant.adapters.mcp.resources import RESOURCE_URIS
from assistant.adapters.mcp.tools import (
    COMPLETE_TASK_TOOL,
    CREATE_TASK_TOOL,
    GET_CASE_TOOL,
    GET_TASK_TOOL,
    SEARCH_KNOWLEDGE_TOOL,
)
from assistant.application.case_service import CaseService
from assistant.application.task_service import CreateTask, TaskService
from assistant.domain.case import Case
from assistant.domain.task import Task, TaskPriority
from assistant.store.actions import SqliteActionRepository
from assistant.store.cases import SqliteCaseRepository
from assistant.store.commitment import SqliteCommitmentRepository
from assistant.store.db import Database
from assistant.store.migrations import apply_migrations
from tests.support.fakes import FakeClock
from tests.support.mcp import (
    KNOWLEDGE_SENTINEL,
    NOW,
    SENSITIVE_SENTINELS,
    McpHarness,
    StubKnowledge,
    config_for,
    seed_sentinels,
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
def harness(database: Database, clock: FakeClock) -> McpHarness:
    return McpHarness(database, clock)


async def _seed(database: Database, clock: FakeClock) -> tuple[Task, Case]:
    """One open task and one open case, so the read tools have something to return."""
    tasks = TaskService(SqliteCommitmentRepository(database), clock)
    task = await tasks.create_task(
        CreateTask(
            title="Write the SE lab report",
            priority=TaskPriority.HIGH,
            estimated_minutes=120,
            due_at=NOW + timedelta(days=2),
        )
    )
    cases = CaseService(
        SqliteCaseRepository(database), SqliteActionRepository(database), clock
    )
    case = await cases.create_case("Register for the course")
    return task, case


async def _read(client: Client, uri: str) -> dict:
    result = await client.read_resource(uri)
    return json.loads(result.contents[0].text)  # type: ignore[attr-defined]


async def _call(client: Client, name: str, arguments: dict) -> tuple[bool, dict | str]:
    result = await client.call_tool(name, arguments)
    text = result.content[0].text  # type: ignore[attr-defined]
    try:
        payload: dict | str = json.loads(text)
    except json.JSONDecodeError:
        payload = text
    return bool(result.is_error), payload


def _counts(database: Database) -> dict[str, int]:
    with database.connect() as connection:
        tables = [
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {
            table: int(
                connection.execute(f"SELECT count(*) AS total FROM {table}").fetchone()[0]
            )
            for table in sorted(tables)
        }


# ------------------------------------------------------------------ exact surface


async def test_the_default_surface_is_exactly_read_only(harness: McpHarness) -> None:
    """§41: two read tools and four resources, and nothing else in any direction."""
    async with Client(harness.server(config_for())) as client:
        tools = [tool.name for tool in (await client.list_tools()).tools]
        resources = [str(item.uri) for item in (await client.list_resources()).resources]

    assert tools == [GET_TASK_TOOL, GET_CASE_TOOL]
    assert resources == list(RESOURCE_URIS)


async def test_knowledge_is_registered_only_when_the_host_opts_in(
    harness: McpHarness,
) -> None:
    async with Client(
        harness.server(config_for(expose_knowledge=True), knowledge=StubKnowledge())
    ) as client:
        tools = [tool.name for tool in (await client.list_tools()).tools]

    assert tools == [GET_TASK_TOOL, GET_CASE_TOOL, SEARCH_KNOWLEDGE_TOOL]


async def test_task_writes_are_registered_only_when_the_scope_allows_them(
    harness: McpHarness,
) -> None:
    async with Client(harness.server(config_for(write_scope="tasks"))) as client:
        tools = [tool.name for tool in (await client.list_tools()).tools]

    assert tools == [
        GET_TASK_TOOL,
        GET_CASE_TOOL,
        CREATE_TASK_TOOL,
        COMPLETE_TASK_TOOL,
    ]


async def test_the_full_surface_is_exact(harness: McpHarness) -> None:
    async with Client(
        harness.server(
            config_for(write_scope="tasks", expose_knowledge=True),
            knowledge=StubKnowledge(),
        )
    ) as client:
        tools = [tool.name for tool in (await client.list_tools()).tools]

    assert tools == [
        GET_TASK_TOOL,
        GET_CASE_TOOL,
        SEARCH_KNOWLEDGE_TOOL,
        CREATE_TASK_TOOL,
        COMPLETE_TASK_TOOL,
    ]


async def test_read_tools_are_annotated_read_only(harness: McpHarness) -> None:
    async with Client(harness.server(config_for(write_scope="tasks"))) as client:
        annotated = {
            tool.name: tool.annotations for tool in (await client.list_tools()).tools
        }

    read = annotated[GET_TASK_TOOL]
    assert read is not None and read.read_only_hint is True
    assert read.open_world_hint is False
    create = annotated[CREATE_TASK_TOOL]
    assert create is not None
    assert create.read_only_hint is False
    assert create.destructive_hint is False
    complete = annotated[COMPLETE_TASK_TOOL]
    assert complete is not None
    assert complete.read_only_hint is False
    # Completing is a terminal transition, so it is conservatively marked destructive and is not
    # claimed to be idempotent.
    assert complete.destructive_hint is True
    assert complete.idempotent_hint is not True


# --------------------------------------------------------------------- resources


async def test_status_reports_counts_and_the_capability_mode(
    harness: McpHarness, database: Database, clock: FakeClock
) -> None:
    await _seed(database, clock)

    async with Client(harness.server(config_for())) as client:
        status = await _read(client, "assistant://status")

    assert status["version"]
    assert status["mcp"] == {
        "transport": "stdio",
        "write_scope": "none",
        "knowledge_exposure": False,
    }
    assert status["counts"] == {
        "open_tasks": 1,
        "open_cases": 1,
        "unread_notifications": 0,
    }
    assert status["capabilities"] == ["read:tasks", "read:cases", "read:plan"]


async def test_open_tasks_are_bounded_and_deterministic(
    harness: McpHarness, database: Database, clock: FakeClock
) -> None:
    task, _ = await _seed(database, clock)

    async with Client(harness.server(config_for())) as client:
        first = await _read(client, "assistant://tasks/open")
        second = await _read(client, "assistant://tasks/open")

    assert first == second
    assert [item["id"] for item in first["tasks"]] == [str(task.id)]
    entry = first["tasks"][0]
    assert entry["title"] == "Write the SE lab report"
    assert entry["priority"] == "high"
    assert entry["status"] == "open"
    assert entry["estimated_minutes"] == 120
    assert entry["deadline"] is not None
    assert set(entry) == {
        "id",
        "title",
        "priority",
        "status",
        "estimated_minutes",
        "deadline",
    }


async def test_open_cases_do_not_expand_their_actions(
    harness: McpHarness, database: Database, clock: FakeClock
) -> None:
    _, case = await _seed(database, clock)

    async with Client(harness.server(config_for())) as client:
        listed = await _read(client, "assistant://cases/open")
        detail = await _call(client, GET_CASE_TOOL, {"reference": str(case.id)[:8]})

    entry = listed["cases"][0]
    assert entry["id"] == str(case.id)
    assert set(entry) == {"id", "title", "status", "created_at", "updated_at"}
    is_error, payload = detail
    assert is_error is False
    assert isinstance(payload, dict)
    assert set(payload) == {
        "id",
        "title",
        "status",
        "created_at",
        "updated_at",
        "completed_at",
        "cancelled_at",
    }


async def test_the_plan_resource_is_honest_when_planning_is_unconfigured(
    harness: McpHarness,
) -> None:
    async with Client(harness.server(config_for(planning=False))) as client:
        plan = await _read(client, "assistant://plan/current")

    assert plan == {
        "configured": False,
        "timezone": None,
        "starts_at": None,
        "ends_at": None,
        "blocks": [],
    }


async def test_the_plan_resource_reads_the_current_week_without_planning(
    harness: McpHarness, database: Database, clock: FakeClock
) -> None:
    await _seed(database, clock)

    async with Client(harness.server(config_for(planning=True))) as client:
        plan = await _read(client, "assistant://plan/current")

    assert plan["configured"] is True
    assert plan["timezone"] == "Asia/Shanghai"
    assert plan["starts_at"] is not None and plan["ends_at"] is not None
    assert plan["blocks"] == []  # the plan was read, never generated
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) AS t FROM plan_proposals").fetchone()["t"] == 0


# ------------------------------------------------------------------------- tools


async def test_get_task_returns_one_task(
    harness: McpHarness, database: Database, clock: FakeClock
) -> None:
    task, _ = await _seed(database, clock)

    async with Client(harness.server(config_for())) as client:
        is_error, payload = await _call(
            client, GET_TASK_TOOL, {"reference": str(task.id)[:8]}
        )

    assert is_error is False
    assert isinstance(payload, dict)
    assert payload["id"] == str(task.id)
    assert payload["description"] is None
    assert set(payload) == {
        "id",
        "title",
        "description",
        "priority",
        "status",
        "estimated_minutes",
        "deadline",
        "created_at",
        "updated_at",
    }


async def test_a_missing_task_is_a_typed_error_without_internals(
    harness: McpHarness,
) -> None:
    async with Client(harness.server(config_for())) as client:
        is_error, payload = await _call(
            client, GET_TASK_TOOL, {"reference": "ffffffff"}
        )

    assert is_error is True
    assert isinstance(payload, dict)
    assert payload["error"]["kind"] == "TaskNotFound"
    message = payload["error"]["message"]
    assert "does not exist" in message
    for forbidden in ("SELECT", "/home/", "Traceback", "sqlite"):
        assert forbidden not in message


async def test_a_blank_reference_is_refused(harness: McpHarness) -> None:
    async with Client(harness.server(config_for())) as client:
        is_error, payload = await _call(client, GET_CASE_TOOL, {"reference": "   "})

    assert is_error is True
    assert isinstance(payload, dict)
    assert payload["error"]["kind"] == "McpInvalidArgument"


async def test_an_ambiguous_prefix_is_refused(
    harness: McpHarness, database: Database
) -> None:
    repository = SqliteCommitmentRepository(database)
    shared = "33333333"
    for index in (1, 2):
        await repository.add_task(
            Task(
                id=UUID(f"{shared}-0000-0000-0000-00000000000{index}"),
                title=f"task {index}",
                created_at=NOW,
                updated_at=NOW,
            )
        )

    async with Client(harness.server(config_for())) as client:
        is_error, payload = await _call(client, GET_TASK_TOOL, {"reference": shared})

    assert is_error is True
    assert isinstance(payload, dict)
    assert payload["error"]["kind"] == "AmbiguousId"


# ------------------------------------------------------------- capability gates


async def test_a_write_tool_does_not_exist_when_the_scope_is_none(
    harness: McpHarness, database: Database
) -> None:
    """§24: the boundary is absence, not a confirmation dialog."""
    before = _counts(database)

    async with Client(harness.server(config_for(write_scope="none"))) as client:
        is_error, payload = await _call(
            client, CREATE_TASK_TOOL, {"title": "A task the client should not create"}
        )

    assert is_error is True
    assert "Unknown tool" in str(payload)
    assert _counts(database) == before


async def test_knowledge_search_does_not_exist_unless_it_is_exposed(
    harness: McpHarness,
) -> None:
    async with Client(harness.server(config_for(expose_knowledge=False))) as client:
        is_error, payload = await _call(
            client, SEARCH_KNOWLEDGE_TOOL, {"query": KNOWLEDGE_SENTINEL}
        )

    assert is_error is True
    assert "Unknown tool" in str(payload)


# ---------------------------------------------------------------- task writes


async def test_creating_a_task_persists_it_through_the_task_service(
    harness: McpHarness, database: Database
) -> None:
    async with Client(harness.server(config_for(write_scope="tasks"))) as client:
        is_error, payload = await _call(
            client,
            CREATE_TASK_TOOL,
            {
                "title": "Book a library room",
                "priority": "high",
                "estimated_minutes": 30,
                "deadline": "2026-09-30T23:59:00+08:00",
            },
        )

    assert is_error is False
    assert isinstance(payload, dict)
    assert payload["status"] == "open"
    assert payload["priority"] == "high"
    with database.connect() as connection:
        row = connection.execute(
            "SELECT title, status FROM tasks WHERE id = ?", (payload["id"],)
        ).fetchone()
    assert row is not None and row["title"] == "Book a library room"


async def test_a_created_task_keeps_the_existing_reminder_and_replan_semantics(
    harness: McpHarness, database: Database
) -> None:
    """§34: the facade calls TaskService, so deadlines still materialize what they always did."""
    async with Client(
        harness.server(config_for(write_scope="tasks", planning=True))
    ) as client:
        is_error, payload = await _call(
            client,
            CREATE_TASK_TOOL,
            {"title": "Submit the report", "deadline": "2026-09-30T23:59:00+08:00"},
        )

    assert is_error is False
    assert isinstance(payload, dict)
    with database.connect() as connection:
        jobs = connection.execute(
            "SELECT count(*) AS total FROM scheduled_jobs"
        ).fetchone()["total"]
        deadline = connection.execute(
            "SELECT due_at FROM deadlines WHERE task_id = ?", (payload["id"],)
        ).fetchone()
    assert deadline is not None
    # The default reminder offsets (a day and two hours before) are materialized by the same
    # mutation the CLI uses, and the rolling replan request is scheduled with it.
    assert jobs >= 3


async def test_a_deadline_without_an_offset_is_refused(
    harness: McpHarness, database: Database
) -> None:
    async with Client(harness.server(config_for(write_scope="tasks"))) as client:
        is_error, payload = await _call(
            client,
            CREATE_TASK_TOOL,
            {"title": "x", "deadline": "2026-09-30T23:59:00"},
        )

    assert is_error is True
    assert isinstance(payload, dict)
    assert payload["error"]["kind"] == "McpInvalidArgument"
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) AS t FROM tasks").fetchone()["t"] == 0


async def test_completing_a_task_uses_the_same_transition_as_the_cli(
    harness: McpHarness, database: Database, clock: FakeClock
) -> None:
    task, _ = await _seed(database, clock)

    async with Client(harness.server(config_for(write_scope="tasks"))) as client:
        first_error, payload = await _call(
            client, COMPLETE_TASK_TOOL, {"reference": str(task.id)[:8]}
        )
        second_error, second_payload = await _call(
            client, COMPLETE_TASK_TOOL, {"reference": str(task.id)[:8]}
        )

    assert first_error is False
    assert isinstance(payload, dict) and payload["status"] == "completed"
    # Completing twice fails, because the transition is terminal: nothing here claims to be
    # idempotent.
    assert second_error is True
    assert isinstance(second_payload, dict)
    assert second_payload["error"]["kind"] in {"TaskNotOpen", "InvalidTaskTransition"}


# ------------------------------------------------------------------- boundaries


async def test_the_registry_has_no_approval_execution_or_side_effect_tool(
    harness: McpHarness,
) -> None:
    """§25/§45: not advertised, not callable, not present under any configuration."""
    forbidden = (
        "challenge",
        "approve",
        "execute",
        "send",
        "submit",
        "ehall",
        "browser",
        "fact",
        "playbook",
        "shell",
        "filesystem",
        "sql",
        "http",
        "fetch",
    )
    configurations = (
        config_for(),
        config_for(write_scope="tasks"),
        config_for(expose_knowledge=True),
        config_for(write_scope="tasks", expose_knowledge=True),
    )
    for config in configurations:
        server = harness.server(config, knowledge=StubKnowledge())
        async with Client(server) as client:
            names = [tool.name for tool in (await client.list_tools()).tools]
            resources = [str(item.uri) for item in (await client.list_resources()).resources]
        for name in names:
            assert not [word for word in forbidden if word in name.lower()], name
        for uri in resources:
            assert not [word for word in forbidden if word in uri.lower()], uri
        assert resources == list(RESOURCE_URIS)


async def test_the_default_surface_leaks_no_sensitive_sentinel(
    harness: McpHarness, database: Database, clock: FakeClock
) -> None:
    """§43: connecting an editor agent must not hand it mail, drafts, facts or playbooks."""
    await _seed(database, clock)
    seed_sentinels(database)

    async with Client(harness.server(config_for())) as client:
        documents = [
            json.dumps(await _read(client, uri), ensure_ascii=False)
            for uri in RESOURCE_URIS
        ]
        for tool in (await client.list_tools()).tools:
            documents.append(json.dumps(tool.model_dump(), ensure_ascii=False))
        for name in (GET_TASK_TOOL, GET_CASE_TOOL):
            _, payload = await _call(client, name, {"reference": "ffffffff"})
            documents.append(json.dumps(payload, ensure_ascii=False))

    blob = "\n".join(documents)
    for sentinel in SENSITIVE_SENTINELS:
        assert sentinel not in blob, sentinel
    assert KNOWLEDGE_SENTINEL not in blob


async def test_an_mcp_task_write_touches_no_approval_execution_fact_or_playbook(
    harness: McpHarness, database: Database, clock: FakeClock
) -> None:
    """§45/§46: a task write is a commitment write and nothing else."""
    await _seed(database, clock)
    seed_sentinels(database)
    before = _counts(database)

    async with Client(harness.server(config_for(write_scope="tasks"))) as client:
        await _call(client, CREATE_TASK_TOOL, {"title": "Another task"})
        await _call(client, COMPLETE_TASK_TOOL, {"reference": "ffffffff"})

    after = _counts(database)
    for table in (
        "approvals",
        "approval_challenges",
        "execution_runs",
        "action_requests",
        "mail_send_links",
        "corrections",
        "fact_candidates",
        "confirmed_facts",
        "playbook_candidates",
        "playbook_replay_tests",
        "playbooks",
        "mail_messages",
        "mail_drafts",
        "web_observations",
    ):
        assert before[table] == after[table], table
    assert after["tasks"] == before["tasks"] + 1


# ------------------------------------------------------------------- knowledge


async def test_knowledge_search_returns_bounded_excerpts(harness: McpHarness) -> None:
    knowledge = StubKnowledge(hits=2)
    server = harness.server(config_for(expose_knowledge=True), knowledge=knowledge)

    async with Client(server) as client:
        is_error, payload = await _call(
            client, SEARCH_KNOWLEDGE_TOOL, {"query": KNOWLEDGE_SENTINEL}
        )

    assert is_error is False
    assert isinstance(payload, dict)
    assert knowledge.calls == [KNOWLEDGE_SENTINEL]
    assert payload["query"] == KNOWLEDGE_SENTINEL
    assert payload["offline_roots"] == ["offline-root"]
    assert len(payload["hits"]) == 2
    total = 0
    for hit in payload["hits"]:
        assert set(hit) == {"logical_uri", "source_span", "excerpt"}
        assert hit["logical_uri"].startswith("local://")
        assert hit["source_span"]
        total += len(hit["excerpt"])
    assert total <= 6000
    assert "file://" not in json.dumps(payload)
    assert "/home/" not in json.dumps(payload)


async def test_knowledge_search_validates_its_inputs(harness: McpHarness) -> None:
    server = harness.server(
        config_for(expose_knowledge=True), knowledge=StubKnowledge()
    )

    async with Client(server) as client:
        blank = await _call(client, SEARCH_KNOWLEDGE_TOOL, {"query": "   "})
        too_long = await _call(client, SEARCH_KNOWLEDGE_TOOL, {"query": "x" * 1001})
        too_many = await _call(
            client, SEARCH_KNOWLEDGE_TOOL, {"query": "alpha", "limit": 9}
        )
        zero = await _call(
            client, SEARCH_KNOWLEDGE_TOOL, {"query": "alpha", "limit": 0}
        )

    for result in (blank, too_long, too_many, zero):
        assert result[0] is True
        assert isinstance(result[1], dict)
        assert result[1]["error"]["kind"] == "McpInvalidArgument"


async def test_knowledge_search_is_a_local_index_read(harness: McpHarness) -> None:
    """§44: the tool calls the deterministic local search and constructs no model client."""
    knowledge = StubKnowledge()
    server = harness.server(config_for(expose_knowledge=True), knowledge=knowledge)

    async with Client(server) as client:
        await _call(client, SEARCH_KNOWLEDGE_TOOL, {"query": "alpha", "root_id": "university"})

    assert knowledge.calls == ["alpha"]


async def test_enabling_knowledge_does_not_add_a_resource(harness: McpHarness) -> None:
    """Knowledge is a tool a client must call, never a resource that arrives with the connection."""
    async with Client(
        harness.server(config_for(expose_knowledge=True), knowledge=StubKnowledge())
    ) as client:
        resources = [str(item.uri) for item in (await client.list_resources()).resources]

    assert resources == list(RESOURCE_URIS)
    assert not [uri for uri in resources if "knowledge" in uri]


async def test_the_knowledge_tool_description_discloses_the_exposure(
    harness: McpHarness,
) -> None:
    async with Client(
        harness.server(config_for(expose_knowledge=True), knowledge=StubKnowledge())
    ) as client:
        tools = {tool.name: tool.description for tool in (await client.list_tools()).tools}

    description = tools[SEARCH_KNOWLEDGE_TOOL] or ""
    assert "local indexed knowledge" in description
    assert "connected MCP client" in description
