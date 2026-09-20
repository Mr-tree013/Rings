"""`pw ask` — a source-grounded answer from indexed personal knowledge (ADR-0019).

Read-only, and the only command in this phase that sends document *content* to a provider. The
command owns composition, not interpretation: retrieval, bounding, citation validation and
source resolution all happen in the application and domain layers, and the CLI renders what it
is given.

Two details are deliberate:

- **no evidence means no provider call.** The context is built first; if nothing indexed matches
  the question, the answer is `INSUFFICIENT_EVIDENCE` and no model client is constructed, so a
  host without `[model]` still gets an honest answer to "there is nothing to quote".
- **every location shown is resolved locally.** The model never produces a path, a page or a line
  range; the renderer looks the cited ids up in the evidence map it built itself.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer
from rich.table import Table

from assistant import bootstrap
from assistant.application.grounded_answer import (
    GroundedAnswerService,
    insufficient_evidence_result,
)
from assistant.application.grounded_context import (
    DEFAULT_EVIDENCE_LIMIT,
    MAX_EVIDENCE_LIMIT,
)
from assistant.cli_support import console, fail
from assistant.domain.config import AssistantConfig
from assistant.domain.errors import (
    DomainError,
    GroundedAnswerInputTooLong,
    GroundedAnswerSemanticError,
    InvalidAssistantConfig,
    ModelCredentialsMissing,
    ModelNotConfigured,
    StorageRootOffline,
)
from assistant.domain.grounded_answer import (
    GroundedAnswerResult,
    GroundedAnswerStatus,
    KnowledgeEvidence,
)

NO_CHANGES_MADE = "No changes were made."


def render_answer(result: GroundedAnswerResult) -> list[str]:
    """Render the answer text with locally appended citation markers."""
    if result.answer.status is GroundedAnswerStatus.INSUFFICIENT_EVIDENCE:
        return []
    lines: list[str] = []
    for segment in result.answer.segments:
        markers = "".join(f"[{source_id}]" for source_id in segment.source_ids)
        lines.append(f"{segment.text} {markers}")
    return lines


def render_sources(result: GroundedAnswerResult) -> list[str]:
    """Render only the evidence the answer actually cited, in first-citation order."""
    return [_source_line(item) for item in result.cited_evidence()]


def _source_line(evidence: KnowledgeEvidence) -> str:
    """`[S1] vault://archive-main/... — lines 18-31`, built from local metadata only."""
    return f"[{evidence.id}] {evidence.logical_uri} - {evidence.location}"


def _load_config_or_fail() -> AssistantConfig:
    try:
        return asyncio.run(bootstrap.config_loader().load())
    except InvalidAssistantConfig as exc:
        fail(f"invalid configuration: {exc}", code=2)


def _validate_limit(limit: int) -> int:
    if not 1 <= limit <= MAX_EVIDENCE_LIMIT:
        fail(f"--limit must be between 1 and {MAX_EVIDENCE_LIMIT}")
    return limit


def ask(
    question: Annotated[
        str, typer.Argument(help="A question to answer from indexed personal knowledge.")
    ],
    root: Annotated[
        str | None,
        typer.Option("--root", help="Search only this configured storage root."),
    ] = None,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            help=f"How many evidence chunks to use (max {MAX_EVIDENCE_LIMIT}).",
        ),
    ] = DEFAULT_EVIDENCE_LIMIT,
) -> None:
    """Answer only from indexed personal knowledge, with citations.

    Answers only from your indexed personal knowledge.

    Retrieved source excerpts and the question are sent to the configured model provider, and
    may incur usage charges. This command is read-only: it does not modify your files or
    assistant state. Offline vault content cannot be used until the vault is connected.
    """
    config = _load_config_or_fail()
    _validate_limit(limit)
    try:
        result = asyncio.run(_answer(config, question, root_id=root, limit=limit))
    except (ModelNotConfigured, ModelCredentialsMissing) as exc:
        fail(str(exc))
    except (GroundedAnswerInputTooLong, GroundedAnswerSemanticError) as exc:
        fail(str(exc))
    except StorageRootOffline as exc:
        fail(str(exc))
    except DomainError as exc:
        fail(str(exc))
    _print_result(result)


async def _answer(
    config: AssistantConfig, question: str, *, root_id: str | None, limit: int
) -> GroundedAnswerResult:
    """Build the context first; only touch the model when there is evidence to ground on."""
    clock = bootstrap.system_clock()
    database = bootstrap.runtime_database(clock)
    builder = bootstrap.grounded_context_builder(clock, database)
    context = await builder.build(question, root_id=root_id, limit=limit)
    if not context.evidence:
        # Nothing indexed matches: an honest answer is already determined, so no model client
        # is built and a host without `[model]` is not asked for a credential it does not need.
        return insufficient_evidence_result(context)
    adapter = bootstrap.model_adapter(config)
    try:
        service: GroundedAnswerService = bootstrap.grounded_answer_service(
            builder, config, model=adapter
        )
        return await service.answer_context(context)
    finally:
        await bootstrap.close_model(adapter)


def _print_result(result: GroundedAnswerResult) -> None:
    """Print the answer, its sources, and the two caveats that matter."""
    if result.answer.status is GroundedAnswerStatus.INSUFFICIENT_EVIDENCE:
        console.print("[bold]Insufficient evidence[/bold]")
        console.print("I couldn't answer this from the indexed evidence.")
        console.print(result.answer.reason or "")
    else:
        console.print("[bold]Answer[/bold]")
        console.print()
        for line in render_answer(result):
            console.print(line)
        console.print()
        sources = render_sources(result)
        if sources:
            console.print("[bold]Sources[/bold]")
            for line in sources:
                console.print(line)
    console.print()
    if result.offline_roots:
        console.print("Offline roots skipped for content evidence:")
        for root_id in result.offline_roots:
            console.print(f"- {root_id}")
    show_metadata = (
        result.metadata_matches
        and result.answer.status is GroundedAnswerStatus.INSUFFICIENT_EVIDENCE
    )
    if show_metadata:
        table = Table(title="Related filenames (no readable content)")
        table.add_column("Root")
        table.add_column("File")
        for entry in result.metadata_matches:
            table.add_row(entry.root_id, entry.relative_path)
        console.print(table)
    console.print(NO_CHANGES_MADE)


def register(app: typer.Typer) -> None:
    """Register `pw ask` on the root app."""
    app.command("ask")(ask)


__all__ = [
    "NO_CHANGES_MADE",
    "ask",
    "register",
    "render_answer",
    "render_sources",
]
