"""The CLI: dispatch, exit codes, and the arguments each command forwards."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from candlegraph.cli import main
from candlegraph.ingestion.runner import IngestResult
from candlegraph.storage import set_db
from tests.conftest import FakeDatabase, make_settings


@pytest.fixture
def cli_env(tokens_file: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDatabase:
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    monkeypatch.setattr("candlegraph.cli.get_settings", lambda: make_settings(tokens_file=str(tokens_file)))
    db = FakeDatabase()
    set_db(db)
    return db


def capture_ingest(monkeypatch: pytest.MonkeyPatch, result: IngestResult) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_run(db: Any, registry: Any, settings: Any, **kwargs: Any) -> IngestResult:
        captured.update(kwargs, registry=registry)
        return result

    monkeypatch.setattr("candlegraph.ingestion.runner.run_ingest", fake_run)
    return captured


# --- argument parsing ------------------------------------------------------


def test_no_command_is_an_error() -> None:
    with pytest.raises(SystemExit) as exc:
        main([])

    assert exc.value.code != 0


def test_an_unknown_command_is_an_error() -> None:
    with pytest.raises(SystemExit):
        main(["bogus"])


# --- serve -----------------------------------------------------------------


def test_serve_starts_uvicorn(cli_env: FakeDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: captured.update(app=app, **kw))

    assert main(["serve"]) == 0
    assert captured["app"] == "candlegraph.api.app:app"


# --- worker ----------------------------------------------------------------


def test_worker_runs_the_worker_loop(cli_env: FakeDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[Any] = []
    monkeypatch.setattr("candlegraph.ingestion.worker.run_worker", lambda s: called.append(s))

    assert main(["worker"]) == 0
    assert len(called) == 1


# --- ingest and backfill ---------------------------------------------------


def test_ingest_runs_one_pass_with_the_configured_window(
    cli_env: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = capture_ingest(monkeypatch, IngestResult(hours_written=5))

    assert main(["ingest"]) == 0
    assert captured["trigger"] == "manual"
    assert captured["lookback_days"] is None


def test_backfill_defaults_to_ninety_days(cli_env: FakeDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    """The phase-2 exit criterion is a 90-day backfill, so that is the default."""
    captured = capture_ingest(monkeypatch, IngestResult(hours_written=5))

    assert main(["backfill"]) == 0
    assert captured["lookback_days"] == 90
    assert captured["trigger"] == "backfill"


def test_backfill_accepts_an_explicit_window(cli_env: FakeDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = capture_ingest(monkeypatch, IngestResult())

    main(["backfill", "--days", "30"])

    assert captured["lookback_days"] == 30


def test_a_run_with_errors_exits_non_zero(cli_env: FakeDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    """CI needs a failed ingest to fail the step."""
    capture_ingest(monkeypatch, IngestResult(errors={"WBTC": "boom"}))

    assert main(["ingest"]) == 1


# --- report ----------------------------------------------------------------


def test_report_prints_the_table(cli_env: FakeDatabase, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["report"]) == 0
    assert "Data-quality report" in capsys.readouterr().out


def test_report_exits_zero_by_default_even_when_dirty(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without --strict the report is informational, not a gate."""
    monkeypatch.setattr("candlegraph.cli.get_settings", lambda: make_settings(tokens_file=str(tokens_file)))
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    set_db(FakeDatabase())

    assert main(["report"]) == 0
    assert "MISSING" in capsys.readouterr().out


def test_strict_report_fails_when_a_token_is_missing(
    cli_env: FakeDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["report", "--strict"]) == 1
    assert "MISSING" in capsys.readouterr().out


def test_strict_report_passes_on_a_clean_database(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    monkeypatch.setattr("candlegraph.cli.get_settings", lambda: make_settings(tokens_file=str(tokens_file)))
    set_db(
        FakeDatabase(
            coverage_report=[
                {
                    "token_id": addr,
                    "symbol": sym,
                    "stored_hours": 25,
                    "first_unix": 1_700_000_000,
                    "last_unix": 1_700_000_000 + 24 * 3600,
                    "expected_hours": 25,
                }
                for addr, sym in (("0xa", "WBTC"), ("0xb", "SHIB"))
            ]
        )
    )

    assert main(["report", "--strict"]) == 0
    assert "OK:" in capsys.readouterr().out


def test_report_window_is_passed_through(cli_env: FakeDatabase, capsys: pytest.CaptureFixture[str]) -> None:
    main(["report", "--days", "7"])

    labels = [label for label, _ in cli_env.fetches]
    assert "coverage_report" in labels
    params = next(p for label, p in cli_env.fetches if label == "coverage_report")
    assert params["start_unix"] is not None


# --- runs ------------------------------------------------------------------


def test_runs_reports_an_empty_ledger(cli_env: FakeDatabase, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["runs"]) == 0
    assert "no ingest runs recorded yet" in capsys.readouterr().out


def test_runs_lists_the_ledger(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("candlegraph.cli.get_settings", lambda: make_settings(tokens_file=str(tokens_file)))
    set_db(
        FakeDatabase(
            get_recent_runs=[
                {
                    "started_at": datetime(2026, 10, 25, 6, 0, tzinfo=UTC),
                    "trigger": "scheduled",
                    "status": "succeeded",
                    "token_hours_written": 409,
                    "pool_hours_written": 120,
                    "duration_seconds": 3.5,
                }
            ]
        )
    )

    assert main(["runs"]) == 0
    out = capsys.readouterr().out
    assert "succeeded" in out
    assert "409" in out


def test_runs_tolerates_an_unfinished_run(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run still in flight has no duration yet."""
    monkeypatch.setattr("candlegraph.cli.get_settings", lambda: make_settings(tokens_file=str(tokens_file)))
    set_db(
        FakeDatabase(
            get_recent_runs=[
                {
                    "started_at": datetime(2026, 10, 25, 6, 0, tzinfo=UTC),
                    "trigger": "manual",
                    "status": "running",
                    "token_hours_written": 0,
                    "pool_hours_written": 0,
                    "duration_seconds": None,
                }
            ]
        )
    )

    assert main(["runs"]) == 0
    assert "running" in capsys.readouterr().out


# --- knowledge ---------------------------------------------------------------


def test_knowledge_sync_assets_upserts_the_registry(
    cli_env: FakeDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["knowledge", "sync-assets"]) == 0

    assert [label for label, _ in cli_env.upserts] == ["upsert_asset"]
    _, rows = cli_env.upserts[0]
    assert [row["symbol"] for row in rows] == ["WBTC", "SHIB"]
    assert "synced 2 assets" in capsys.readouterr().out


def test_knowledge_without_a_subcommand_is_an_error(cli_env: FakeDatabase) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["knowledge"])

    assert exc.value.code != 0


def test_knowledge_embed_reports_the_backlog_and_what_it_embedded(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    monkeypatch.setattr(
        "candlegraph.cli.get_settings",
        lambda: make_settings(tokens_file=str(tokens_file), embedding_provider="fake"),
    )
    set_db(FakeDatabase(count_embedding_backlog=[{"missing": 0, "stale": 0, "total": 7}]))

    assert main(["knowledge", "embed"]) == 0

    out = capsys.readouterr().out
    assert "fake-hash (fake): 0 of 7 chunks to embed" in out
    assert "embedded 0 chunks in 0 batches; 0 remaining" in out


def test_knowledge_reembed_is_the_same_command_under_the_name_the_rollout_uses(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    monkeypatch.setattr(
        "candlegraph.cli.get_settings",
        lambda: make_settings(tokens_file=str(tokens_file), embedding_provider="fake"),
    )
    set_db(FakeDatabase(count_embedding_backlog=[{"missing": 0, "stale": 0, "total": 0}]))

    assert main(["knowledge", "reembed", "--batch-size", "4", "--limit", "8"]) == 0
    assert "embedded 0 chunks" in capsys.readouterr().out


def test_knowledge_embed_with_a_misconfigured_provider_exits_with_the_message(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    monkeypatch.setattr(
        "candlegraph.cli.get_settings",
        lambda: make_settings(tokens_file=str(tokens_file), embedding_provider="voyage"),
    )
    set_db(FakeDatabase())

    with pytest.raises(SystemExit, match="EMBEDDING_PROVIDER"):
        main(["knowledge", "embed"])


def test_knowledge_fetch_news_prints_outcomes_and_exits_zero_when_clean(
    cli_env: FakeDatabase, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from candlegraph.knowledge.news import FetchOutcome, NewsFetchSummary

    captured: dict[str, Any] = {}

    def fake(db: Any, settings: Any, symbols: Any = None) -> NewsFetchSummary:
        captured["symbols"] = symbols
        return NewsFetchSummary(
            day="2026-11-01",
            outcomes=[
                FetchOutcome("WBTC", "ok", credits=1, results=5, documents_stored=3, chunks_written=9),
                FetchOutcome("SHIB", "budget_refused", message="daily cap reached"),
            ],
        )

    monkeypatch.setattr("candlegraph.knowledge.news.run_news_fetch", fake)

    assert main(["knowledge", "fetch-news", "--symbol", "WBTC", "--symbol", "SHIB"]) == 0

    out = capsys.readouterr().out
    assert captured["symbols"] == ["WBTC", "SHIB"]
    assert "budget_refused" in out and "daily cap reached" in out
    assert "spent 1 credits, stored 3 documents" in out


def test_knowledge_fetch_news_exits_non_zero_on_a_failed_run(
    cli_env: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    from candlegraph.knowledge.news import FetchOutcome, NewsFetchSummary

    monkeypatch.setattr(
        "candlegraph.knowledge.news.run_news_fetch",
        lambda db, settings, symbols=None: NewsFetchSummary(
            day="2026-11-01", outcomes=[FetchOutcome("WBTC", "rate_limited", message="HTTP 429")]
        ),
    )

    assert main(["knowledge", "fetch-news"]) == 1


def test_knowledge_fetch_news_without_a_key_exits_with_the_message(cli_env: FakeDatabase) -> None:
    with pytest.raises(SystemExit, match="TAVILY_API_KEY"):
        main(["knowledge", "fetch-news"])


def test_knowledge_search_runs_a_lexical_retrieval_bounded_at_the_bar(
    cli_env: FakeDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    cli_env.reads["lexical_search"] = [
        {
            "id": 1,
            "document_id": 1,
            "ordinal": 0,
            "text": "WBTC slipped on ETF outflows.",
            "url": "https://x.com/a",
            "title": "Outflows",
            "published_at": datetime(2026, 10, 24, 9, 0, tzinfo=UTC),
            "retrieved_at": datetime(2026, 10, 24, 14, 0, tzinfo=UTC),
            "score": 1 / 61,
            "dense_rank": None,
            "lexical_rank": 1,
        }
    ]

    assert (
        main(
            [
                "knowledge",
                "search",
                "outflows",
                "--symbol",
                "WBTC",
                "--at",
                "2026-10-24T14:00:00+00:00",
                "--lexical-only",
            ]
        )
        == 0
    )

    out = capsys.readouterr().out
    assert "1 chunks as of 2026-10-24 14:00 UTC (display; lexical only)" in out
    assert "https://x.com/a" in out
    ((_, params),) = [(name, p) for name, p in cli_env.fetches if name == "lexical_search"]
    assert params["asset_id"] == "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"
    assert params["tsquery"] == '"WBTC"'


def test_knowledge_search_refuses_an_untracked_symbol(cli_env: FakeDatabase) -> None:
    with pytest.raises(SystemExit, match="tracked: WBTC, SHIB"):
        main(["knowledge", "search", "q", "--symbol", "DOGE", "--lexical-only"])


def test_knowledge_search_refuses_an_unbuilt_backend_with_the_evaluation(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    monkeypatch.setattr(
        "candlegraph.cli.get_settings",
        lambda: make_settings(tokens_file=str(tokens_file), retrieval_backend="ragflow"),
    )
    set_db(FakeDatabase())

    with pytest.raises(SystemExit, match=r"ragflow/DESIGN\.md"):
        main(["knowledge", "search", "q", "--lexical-only"])


def test_knowledge_link_mentions_backfills_and_reports(
    cli_env: FakeDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    cli_env.reads["get_documents_without_mentions"] = [{"id": 1}]
    cli_env.reads["get_chunks"] = [{"id": 5, "text": "WBTC moved"}]

    assert main(["knowledge", "link-mentions"]) == 0

    assert "linked 1 documents, 1 new mentions" in capsys.readouterr().out
    assert [label for label, _ in cli_env.upserts] == ["upsert_asset", "upsert_mentions"]
