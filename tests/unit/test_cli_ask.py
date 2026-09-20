"""`pw ask`: citation rendering, refusal paths and read-only behaviour (ADR-0019)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from assistant import bootstrap
from assistant.adapters.model.fake import FakeModelAdapter
from assistant.cli import app
from assistant.cli_ask import NO_CHANGES_MADE, render_answer, render_sources
from assistant.domain.errors import ModelRateLimited
from assistant.domain.grounded_answer import (
    EvidenceId,
    GroundedAnswer,
    GroundedAnswerResult,
    GroundedAnswerSegment,
    GroundedAnswerStatus,
    KnowledgeEvidence,
)
from assistant.domain.knowledge import SourceSpan
from assistant.domain.storage import StorageUri
from tests.support.fakes import FakeClock

runner = CliRunner()
START = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
CONFIG = "\n".join(
    (
        "format_version = 1",
        "",
        "[model]",
        'provider = "deepseek"',
        'model = "deepseek-flash"',
        "",
    )
)
NOTICE = "\n".join(
    [
        f"filler line {index}"
        for index in range(1, 20)
    ]
    + ["The SE lab submission deadline is October 23 at 23:59.", "More filler."]
)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=START)


@pytest.fixture
def workspace(
    tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A configured host with one indexed local root, ready for `pw ask`.

    The root is scanned and indexed through the same composition the CLI uses, so the test
    exercises the real wiring instead of a private setup path.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    documents = tmp_path / "documents" / "university"
    documents.mkdir(parents=True, exist_ok=True)
    (documents / "notice.md").write_text(NOTICE, encoding="utf-8")
    config_directory = tmp_path / "config" / "growing-assistant"
    config_directory.mkdir(parents=True, exist_ok=True)
    (config_directory / "config.toml").write_text(
        "\n".join(
            (
                CONFIG.rstrip(),
                "",
                "[[storage.roots]]",
                'kind = "local"',
                'id = "university"',
                'label = "University"',
                f'path = "{documents}"',
                "enabled = true",
                "",
            )
        ),
        encoding="utf-8",
    )
    database = bootstrap.runtime_database(clock)
    _run(
        bootstrap.catalog_service(clock, database).scan_local(
            root_id="university", label="University", path=documents
        )
    )
    _run(bootstrap.knowledge_indexer(clock, database).index_root("university"))
    return tmp_path


def _env(tmp_path: Path, *, model: bool = True, key: bool = True) -> dict[str, str]:
    """The environment for one `pw ask` run.

    The model-less variant uses its own config directory, so it never rewrites the host config
    the other variants read.
    """
    config_home = tmp_path / "config"
    if not model:
        config_home = tmp_path / "config-without-model"
        directory = config_home / "growing-assistant"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "config.toml").write_text(
            "\n".join(
                (
                    "format_version = 1",
                    "",
                    "[[storage.roots]]",
                    'kind = "local"',
                    'id = "university"',
                    'label = "University"',
                    f'path = "{tmp_path / "documents" / "university"}"',
                    "enabled = true",
                    "",
                )
            ),
            encoding="utf-8",
        )
    env = {
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(config_home),
        "COLUMNS": "200",
    }
    if key:
        env["DEEPSEEK_API_KEY"] = "test-secret"
    return env


def _install_model(monkeypatch: pytest.MonkeyPatch, *answers: object) -> FakeModelAdapter:
    model = FakeModelAdapter()
    for answer in answers:
        if isinstance(answer, Exception):
            model.queue_error(answer)
        else:
            model.queue_text(str(answer))
    monkeypatch.setattr(bootstrap, "model_adapter", lambda config: model)
    return model


def _answered(*segments: tuple[str, list[str]]) -> str:
    return json.dumps(
        {
            "status": "answered",
            "segments": [
                {"text": text, "source_ids": source_ids} for text, source_ids in segments
            ],
            "reason": None,
        }
    )


def _run(coroutine: object) -> object:
    import asyncio

    return asyncio.run(coroutine)  # type: ignore[arg-type]


# --------------------------------------------------------------------- rendering


def _evidence(source_id: str, *, page: int | None = None) -> KnowledgeEvidence:
    span = SourceSpan.page(page) if page is not None else SourceSpan.lines(18, 31)
    return KnowledgeEvidence(
        id=EvidenceId(source_id),
        root_id="archive-main",
        entry_id=uuid4(),
        chunk_id=uuid4(),
        logical_uri=StorageUri.parse(
            f"vault://archive-main/Courses/SE/{'report.pdf' if page else 'notice.md'}"
        ),
        source_span=span,
        content="...",
    )


def test_answer_rendering_appends_local_citation_markers() -> None:
    result = GroundedAnswerResult(
        answer=GroundedAnswer(
            status=GroundedAnswerStatus.ANSWERED,
            segments=(
                GroundedAnswerSegment("Deadline is October 23.", (EvidenceId("S1"),)),
                GroundedAnswerSegment("The start differs.", (EvidenceId("S1"), EvidenceId("S2"))),
            ),
        ),
        evidence=(_evidence("S1"), _evidence("S2", page=4)),
    )

    assert render_answer(result) == [
        "Deadline is October 23. [S1]",
        "The start differs. [S1][S2]",
    ]


def test_sources_list_only_cited_evidence_once_in_first_citation_order() -> None:
    result = GroundedAnswerResult(
        answer=GroundedAnswer(
            status=GroundedAnswerStatus.ANSWERED,
            segments=(
                GroundedAnswerSegment("Second source first.", (EvidenceId("S2"),)),
                GroundedAnswerSegment("Then both.", (EvidenceId("S1"), EvidenceId("S2"))),
            ),
        ),
        evidence=(_evidence("S1"), _evidence("S2", page=4), _evidence("S3")),
    )

    sources = render_sources(result)

    assert len(sources) == 2  # S3 was available but never cited
    assert sources[0].startswith("[S2] vault://archive-main/Courses/SE/report.pdf - page 4")
    assert sources[1].startswith("[S1] vault://archive-main/Courses/SE/notice.md - lines 18-31")


# -------------------------------------------------------------------------- CLI


def test_ask_answers_from_real_indexed_knowledge(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _install_model(
        monkeypatch, _answered(("The submission deadline is October 23 at 23:59.", ["S1"]))
    )

    result = runner.invoke(
        app, ["ask", "What is the SE lab submission deadline?"], env=_env(workspace)
    )

    assert result.exit_code == 0, result.output
    assert "Answer" in result.output
    assert "The submission deadline is October 23 at 23:59. [S1]" in result.output
    assert "Sources" in result.output
    assert "[S1] local://university/notice.md - lines " in result.output
    assert NO_CHANGES_MADE in result.output
    span = re.search(r"\[S1\] \S+ - lines (\d+)-(\d+)", result.output)
    assert span is not None
    assert int(span.group(1)) <= 20 <= int(span.group(2))  # the notice's deadline line
    body = json.loads(model.requests[0].messages[0].content)
    assert body["question"] == "What is the SE lab submission deadline?"
    assert "October 23" in body["evidence"][0]["content"]


def test_ask_uses_the_question_as_the_only_search_query(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_model(monkeypatch, _answered(("October 23.", ["S1"])))

    runner.invoke(app, ["ask", "deadline"], env=_env(workspace))

    # A question with no matching text produces no evidence, so the model is never called.
    model = _install_model(monkeypatch)
    unmatched = runner.invoke(app, ["ask", "quantum chromodynamics"], env=_env(workspace))

    assert unmatched.exit_code == 0, unmatched.output
    assert "I couldn't answer this from the indexed evidence." in unmatched.output
    assert model.requests == []


def test_ask_reports_no_evidence_without_a_model_call_even_when_unconfigured(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _install_model(monkeypatch)

    result = runner.invoke(
        app, ["ask", "quantum chromodynamics"], env=_env(workspace, model=False, key=False)
    )

    assert result.exit_code == 0, result.output
    assert "Insufficient evidence" in result.output
    assert model.requests == []


def test_ask_requires_a_model_section_when_there_is_evidence(workspace: Path) -> None:
    """No fake adapter is installed here: the real composition decides."""
    result = runner.invoke(app, ["ask", "deadline"], env=_env(workspace, model=False))

    assert result.exit_code == 1
    assert "no [model] section" in result.output
    assert "Answer" not in result.output


def test_ask_requires_a_key_when_there_is_evidence(workspace: Path) -> None:
    result = runner.invoke(app, ["ask", "deadline"], env=_env(workspace, key=False))

    assert result.exit_code == 1
    assert "DEEPSEEK_API_KEY" in result.output
    assert "Answer" not in result.output


def test_ask_reports_provider_failures(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_model(monkeypatch, ModelRateLimited("the provider is rate limiting (429)"))

    result = runner.invoke(app, ["ask", "deadline"], env=_env(workspace))

    assert result.exit_code == 1
    assert "429" in result.output
    assert "Answer" not in result.output


def test_ask_rejects_an_invalid_citation(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_model(monkeypatch, _answered(("October 23.", ["S9"])))

    result = runner.invoke(app, ["ask", "deadline"], env=_env(workspace))

    assert result.exit_code == 1
    assert "S9" in result.output
    assert "Answer" not in result.output


def test_ask_reports_malformed_model_output(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_model(monkeypatch, "Here you go: October 23.")

    result = runner.invoke(app, ["ask", "deadline"], env=_env(workspace))

    assert result.exit_code == 1
    assert "not valid JSON" in result.output
    assert "Traceback" not in result.output


def test_ask_validates_the_limit_and_the_root(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_model(monkeypatch, _answered(("October 23.", ["S1"])))

    too_large = runner.invoke(app, ["ask", "deadline", "--limit", "99"], env=_env(workspace))
    too_small = runner.invoke(app, ["ask", "deadline", "--limit", "0"], env=_env(workspace))
    unknown_root = runner.invoke(
        app, ["ask", "deadline", "--root", "nope"], env=_env(workspace)
    )

    assert too_large.exit_code == 1
    assert "--limit must be between 1 and 20" in too_large.output
    assert too_small.exit_code == 1
    assert unknown_root.exit_code == 0, unknown_root.output
    assert "I couldn't answer this from the indexed evidence." in unknown_root.output


def test_ask_reports_an_offline_root(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _install_model(monkeypatch)
    import shutil

    shutil.rmtree(workspace / "documents" / "university")

    result = runner.invoke(app, ["ask", "deadline"], env=_env(workspace))

    assert result.exit_code == 0, result.output
    assert "Offline roots skipped for content evidence:" in result.output
    assert "- university" in result.output
    assert model.requests == []


def test_ask_restricts_search_to_the_requested_root(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _install_model(monkeypatch, _answered(("October 23.", ["S1"])))

    scoped = runner.invoke(
        app, ["ask", "deadline", "--root", "university"], env=_env(workspace)
    )

    assert scoped.exit_code == 0, scoped.output
    payload = json.loads(model.requests[0].messages[0].content)
    uris = [item["logical_uri"] for item in payload["evidence"]]
    assert all(uri.startswith("local://university/") for uri in uris)


def test_ask_has_no_write_or_execute_options(workspace: Path) -> None:
    for flag in ("--apply", "--execute", "--write", "--web", "--tools"):
        result = runner.invoke(app, ["ask", "deadline", flag], env=_env(workspace))
        assert result.exit_code != 0, flag


def test_ask_help_states_what_is_sent_and_that_it_is_read_only() -> None:
    result = runner.invoke(app, ["ask", "--help"])

    assert result.exit_code == 0
    assert "only from your indexed personal knowledge" in result.output
    assert "read-only" in result.output
    assert "Offline vault content cannot be used" in result.output
    assert "usage charges" in result.output


def test_ask_never_reads_mutation_state(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The grounded path is knowledge-only: no task, notification or scheduler data."""
    model = _install_model(monkeypatch, _answered(("October 23.", ["S1"])))
    env = _env(workspace)
    runner.invoke(
        app,
        ["task", "add", "TASK-SECRET", "--description", "TASK-SECRET"],
        env=env,
    )

    result = runner.invoke(app, ["ask", "deadline"], env=env)

    assert result.exit_code == 0, result.output
    sent = model.requests[0].messages[0].content
    assert "TASK-SECRET" not in sent
    assert result.output.count("TASK-SECRET") == 0
