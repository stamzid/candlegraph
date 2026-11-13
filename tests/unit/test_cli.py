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
    monkeypatch.setattr("candlegraph.utils.ports.port_is_free", lambda _h, _p: True)

    assert main(["serve"]) == 0
    assert captured["app"] == "candlegraph.api.app:app"
    assert captured["port"] == 8000


def test_serve_refuses_a_busy_port_and_suggests_free_ones(
    cli_env: FakeDatabase, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The compose API holds APP_HOST_PORT=8000; a local serve used to die in uvicorn's bind."""
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: pytest.fail("must not start uvicorn on a busy port"))
    monkeypatch.setattr("candlegraph.utils.ports.port_is_free", lambda _h, port: port != 8000)

    with caplog.at_level("ERROR", logger="candlegraph"):
        assert main(["serve"]) == 1

    assert "8000 is already in use" in caplog.text
    assert "--port 8001" in caplog.text


def test_serve_port_auto_takes_the_first_free_suggestion(
    cli_env: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: captured.update(kw))
    monkeypatch.setattr("candlegraph.utils.ports.port_is_free", lambda _h, port: port not in (8000, 8001))

    assert main(["serve", "--port", "auto", "--host", "127.0.0.1"]) == 0
    assert captured["port"] == 8080
    assert captured["host"] == "127.0.0.1"


def test_serve_takes_an_explicit_port(cli_env: FakeDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: captured.update(kw))
    monkeypatch.setattr("candlegraph.utils.ports.port_is_free", lambda _h, _p: True)

    assert main(["serve", "--port", "9123"]) == 0
    assert captured["port"] == 9123
    assert main(["serve", "--port", "eighty"]) == 2


# --- worker ----------------------------------------------------------------


def test_worker_runs_the_process_loop_with_the_worker_jobs(
    cli_env: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[Any] = []
    monkeypatch.setattr(
        "candlegraph.harness.process.run_process", lambda s, jobs, **k: called.append((jobs, k))
    )

    assert main(["worker"]) == 0
    ((jobs, kwargs),) = called
    assert [job.name for job in jobs] == ["ingest"]
    assert kwargs.get("shutdown_wait", True) is True


def test_worker_takes_an_explicit_job_list_and_rejects_unknown_names(
    cli_env: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[Any] = []
    monkeypatch.setattr("candlegraph.harness.process.run_process", lambda s, jobs, **k: called.append(jobs))

    assert main(["worker", "--jobs", "ingest,observer"]) == 0
    assert [job.name for job in called[0]] == ["ingest", "observer"]
    assert main(["worker", "--jobs", "nope"]) == 2


def test_jobs_lists_every_registered_job(cli_env: FakeDatabase, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["jobs"]) == 0
    out = capsys.readouterr().out

    for name, setting in (
        ("ingest", "DATA_POLL_INTERVAL"),
        ("observer", "OBSERVER_INTERVAL"),
        ("janitor", "JANITOR_INTERVAL"),
    ):
        assert name in out and setting in out
    assert "market_data" in out


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


def test_report_json_prints_the_structure_the_mcp_tool_returns(
    cli_env: FakeDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    """``report --json`` is the reference the MCP exit criterion names."""
    import json

    assert main(["report", "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)

    assert parsed["missing_tokens"] == ["WBTC", "SHIB"]
    assert parsed["ok"] is False
    assert parsed["tokens"] == []


def test_report_json_and_strict_compose(cli_env: FakeDatabase, capsys: pytest.CaptureFixture[str]) -> None:
    import json

    assert main(["report", "--json", "--strict"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


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


# --- mcp -------------------------------------------------------------------


def test_mcp_serves_over_stdio_by_default(cli_env: FakeDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr("candlegraph.mcp.server.run_server", lambda **kwargs: captured.update(kwargs))

    assert main(["mcp"]) == 0

    assert captured == {"transport": "stdio", "host": "127.0.0.1", "port": 8900}


def test_mcp_can_serve_over_http_for_a_client_in_another_container(
    cli_env: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr("candlegraph.mcp.server.run_server", lambda **kwargs: captured.update(kwargs))

    assert main(["mcp", "--transport", "http", "--host", "0.0.0.0", "--port", "9100"]) == 0

    assert captured == {"transport": "http", "host": "0.0.0.0", "port": 9100}


def test_mcp_without_the_extra_says_how_to_install_it(
    cli_env: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `ui` command's rule: an optional extra explains itself, never traces."""
    import builtins

    real_import = builtins.__import__

    def refuse(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "candlegraph.mcp.server":
            raise ImportError("No module named 'mcp'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)

    with pytest.raises(SystemExit, match=r"uv sync --extra mcp"):
        main(["mcp"])


def test_an_unknown_mcp_transport_is_rejected_by_the_parser(cli_env: FakeDatabase) -> None:
    with pytest.raises(SystemExit):
        main(["mcp", "--transport", "carrier-pigeon"])


# --- the fill cache --------------------------------------------------------


def fill_env(tokens_file: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDatabase:
    """A database with one token whose history has a two-hour hole."""
    from decimal import Decimal

    hour = 3600
    base = 1_700_000_000 // (24 * hour) * (24 * hour)
    rows = [
        {
            "symbol": "WBTC",
            "token_id": "0xwbtc",
            "period_start_unix": base + i * hour,
            "timestamp": datetime.fromtimestamp(base + i * hour, tz=UTC),
            "open": Decimal(str(100 + i)),
            "high": Decimal(str(100 + i)),
            "low": Decimal(str(100 + i)),
            "close": Decimal(str(100 + i)),
            "price_usd": Decimal(str(100 + i)),
            "volume_usd": Decimal("1000"),
            "total_value_locked_usd": Decimal("5000"),
        }
        for i in (0, 1, 2, 5, 6, 7)
    ]
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    monkeypatch.setattr("candlegraph.cli.get_settings", lambda: make_settings(tokens_file=str(tokens_file)))
    db = FakeDatabase(
        get_price_history=rows,
        coverage_report=[
            {
                "token_id": "0xwbtc",
                "symbol": "WBTC",
                "stored_hours": 6,
                "expected_hours": 8,
                "first_unix": base,
                "last_unix": base + 7 * hour,
            }
        ],
    )
    set_db(db)
    return db


def test_fill_refreshes_the_cache_and_says_it_is_display_only(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command exists so the capability is not MCP- or UI-only.

    It also has to say what it built: a reader who does not know that linear
    interpolation looks ahead would otherwise reasonably assume a backtest
    picks these rows up.
    """
    db = fill_env(tokens_file, monkeypatch)

    assert main(["fill"]) == 0

    out = capsys.readouterr().out
    assert "2 hour(s) written" in out
    assert "display only" in out and "looks ahead" in out
    assert "Nothing here is read by" in out
    assert any(label == "insert_token_hours_fill" for label, _ in db.upserts)


def test_fill_defaults_to_every_token_with_stored_history(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fill_env(tokens_file, monkeypatch)

    assert main(["fill"]) == 0
    assert "1 symbol(s)" in capsys.readouterr().out


def test_fill_status_reports_an_empty_cache_without_failing(
    cli_env: FakeDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["fill", "--status"]) == 0
    assert "fill cache is empty" in capsys.readouterr().out


def test_fill_clear_deletes_instead_of_recomputing(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = fill_env(tokens_file, monkeypatch)

    assert main(["fill", "--clear"]) == 0

    assert "cleared" in capsys.readouterr().out
    assert [label for label, _ in db.executed] == ["delete_fills_for_method"]
    assert db.upserts == [], "clearing must not write"


def test_fill_with_no_stored_history_says_so_rather_than_failing(
    cli_env: FakeDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["fill"]) == 0
    assert "no stored history" in capsys.readouterr().out


def test_fill_computes_one_estimator_at_a_time(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """They are alternatives for the same hour, so --method takes one value."""
    db = fill_env(tokens_file, monkeypatch)

    assert main(["fill", "--method", "bridge"]) == 0

    out = capsys.readouterr().out
    assert "fill cache (bridge" in out
    written = [r for label, r in db.upserts if label == "insert_token_hours_fill"]
    assert written and all(row["method"] == "bridge" for row in written[0])


def test_fill_says_whether_the_chosen_estimator_looks_ahead(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fill_env(tokens_file, monkeypatch)
    assert main(["fill", "--method", "causal"]) == 0
    assert "safe for a strategy" in capsys.readouterr().out

    fill_env(tokens_file, monkeypatch)
    assert main(["fill", "--method", "bridge"]) == 0
    assert "looks ahead" in capsys.readouterr().out


def test_fill_all_methods_computes_each_in_turn(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """So the same gap holds all three and they can be compared."""
    db = fill_env(tokens_file, monkeypatch)

    assert main(["fill", "--all-methods"]) == 0

    out = capsys.readouterr().out
    assert all(f"fill cache ({m}" in out for m in ("linear", "causal", "bridge"))
    methods = {row["method"] for _, rows in db.upserts for row in rows}
    assert methods == {"linear", "causal", "bridge"}


def test_fill_clear_scopes_to_the_chosen_estimator(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Clearing one must not destroy the comparison with the others."""
    db = fill_env(tokens_file, monkeypatch)

    assert main(["fill", "--clear", "--method", "causal"]) == 0

    cleared = [params for label, params in db.executed if label == "delete_fills_for_method"]
    assert [params["method"] for params in cleared] == ["causal"]
    assert "causal" in capsys.readouterr().out


def test_fill_clear_all_methods_empties_every_estimator(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = fill_env(tokens_file, monkeypatch)

    assert main(["fill", "--clear", "--all-methods"]) == 0

    cleared = [params["method"] for label, params in db.executed if label == "delete_fills_for_method"]
    assert cleared == ["linear", "causal", "bridge"]


def test_fill_rejects_an_estimator_that_does_not_exist(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fill_env(tokens_file, monkeypatch)
    with pytest.raises(SystemExit):
        main(["fill", "--method", "telepathy"])


def test_fill_evaluate_scores_every_estimator_against_known_prices(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ "Which is best" answered by holding out real hours, not by taste."""
    from decimal import Decimal

    hour = 3600
    base = 1_700_000_000 // (24 * hour) * (24 * hour)
    rng = __import__("numpy").random.default_rng(3)
    walk = 100 * __import__("numpy").exp(rng.normal(0, 0.01, 900).cumsum())
    rows = [
        {
            "symbol": "WBTC",
            "token_id": "0xwbtc",
            "period_start_unix": base + i * hour,
            "timestamp": datetime.fromtimestamp(base + i * hour, tz=UTC),
            "open": Decimal(str(p)),
            "high": Decimal(str(p)),
            "low": Decimal(str(p)),
            "close": Decimal(str(p)),
            "price_usd": Decimal(str(p)),
            "volume_usd": Decimal("1000"),
            "total_value_locked_usd": Decimal("5000"),
        }
        for i, p in enumerate(walk)
    ]
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    monkeypatch.setattr("candlegraph.cli.get_settings", lambda: make_settings(tokens_file=str(tokens_file)))
    db = FakeDatabase(
        get_price_history=rows,
        coverage_report=[
            {
                "token_id": "0xwbtc",
                "symbol": "WBTC",
                "stored_hours": 900,
                "expected_hours": 900,
                "first_unix": base,
                "last_unix": base + 899 * hour,
            }
        ],
    )
    set_db(db)

    assert main(["fill", "--evaluate"]) == 0

    out = capsys.readouterr().out
    assert all(method in out for method in ("linear", "causal", "bridge"))
    assert "MAE" in out and "held-out hours" in out
    assert "not a licence to trade on it" in out
    assert db.upserts == [], "evaluating writes nothing"


def test_fill_status_lists_what_each_estimator_does(
    cli_env: FakeDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["fill", "--status"]) == 0
    assert "fill cache is empty" in capsys.readouterr().out


def test_the_parser_offers_exactly_the_estimators_that_exist() -> None:
    """The parser names them without importing ingestion at start-up.

    Two lists that must agree, so one test holds them together rather than
    a comment asking nicely.
    """
    from candlegraph.cli import FILL_METHODS
    from candlegraph.ingestion.fills import METHOD_POLICY, METHODS

    assert FILL_METHODS == METHODS
    assert set(FILL_METHODS) == set(METHOD_POLICY)


# --- simulation --------------------------------------------------------------


def simulate_env(tokens_file: Path, monkeypatch: pytest.MonkeyPatch, bars: int = 500) -> FakeDatabase:
    """Enough history for resampling to mean something."""
    from decimal import Decimal

    import numpy as np

    hour = 3600
    base = 1_700_000_000 // (24 * hour) * (24 * hour)
    rng = np.random.default_rng(4)
    walk = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, bars)))
    rows = [
        {
            "symbol": "WBTC",
            "token_id": "0xwbtc",
            "period_start_unix": base + i * hour,
            "timestamp": datetime.fromtimestamp(base + i * hour, tz=UTC),
            "open": Decimal(str(p)),
            "high": Decimal(str(p)),
            "low": Decimal(str(p)),
            "close": Decimal(str(p)),
            "price_usd": Decimal(str(p)),
            "volume_usd": Decimal("1000"),
            "total_value_locked_usd": Decimal("5000"),
        }
        for i, p in enumerate(walk)
    ]
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    monkeypatch.setattr("candlegraph.cli.get_settings", lambda: make_settings(tokens_file=str(tokens_file)))
    db = FakeDatabase(get_price_history=rows)
    set_db(db)
    return db


def test_simulate_prints_a_distribution_with_a_p_value(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The step's exit criterion, as a command."""
    db = simulate_env(tokens_file, monkeypatch)

    assert main(["simulate", "WBTC", "--strategy", "bollinger", "--paths", "40", "--seed", "7"]) == 0

    out = capsys.readouterr().out
    assert "40 paths" in out and "generator 'block'" in out and "seed 7" in out
    assert "p-value" in out
    assert "actual excess vs buy & hold" in out
    assert db.upserts == [], "simulating writes nothing"


def test_simulate_repeats_the_caveat_about_the_sample(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Resampling widens the sample along one axis only, and every surface
    that reports a distribution has to say so."""
    simulate_env(tokens_file, monkeypatch)

    assert main(["simulate", "WBTC", "--paths", "30"]) == 0

    assert "still ninety days" in capsys.readouterr().out


def test_simulate_defaults_to_the_block_bootstrap(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Not IID: that would destroy the clustering the strategies trade."""
    simulate_env(tokens_file, monkeypatch)

    assert main(["simulate", "WBTC", "--paths", "20"]) == 0

    assert "generator 'block'" in capsys.readouterr().out


def test_simulate_accepts_each_generator(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for method in ("block", "iid", "gbm", "regime"):
        simulate_env(tokens_file, monkeypatch)
        assert main(["simulate", "WBTC", "--paths", "20", "--method", method]) == 0
        assert f"generator {method!r}" in capsys.readouterr().out


def test_simulate_rejects_a_generator_that_does_not_exist(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    simulate_env(tokens_file, monkeypatch)
    with pytest.raises(SystemExit):
        main(["simulate", "WBTC", "--method", "crystal-ball"])


def test_simulate_refuses_a_window_too_short_to_resample(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    simulate_env(tokens_file, monkeypatch, bars=20)
    with pytest.raises(SystemExit, match="usable returns"):
        main(["simulate", "WBTC", "--paths", "10"])


def test_simulate_execution_reports_the_reconciliation_first(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Everything after it is only meaningful if the engines agree."""
    simulate_env(tokens_file, monkeypatch)

    assert main(["simulate", "WBTC", "--execution"]) == 0

    out = capsys.readouterr().out
    assert "Reconciliation against the vectorised backtester" in out
    assert "AGREES" in out, "the two engines must match at zero latency"
    assert "What the convenient assumptions are worth" in out
    assert "latency_bars" in out


def test_the_parser_offers_exactly_the_generators_that_exist() -> None:
    """Two lists that must agree, held together rather than asked nicely."""
    from candlegraph.analytics.simulation import GENERATOR_NAMES
    from candlegraph.cli import DEFAULT_GENERATOR, SIMULATION_GENERATORS

    assert SIMULATION_GENERATORS == GENERATOR_NAMES
    assert DEFAULT_GENERATOR == "block"


def test_forecast_posterior_reports_r_hat_and_both_intervals(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The stage's third exit clause, as a command."""
    simulate_env(tokens_file, monkeypatch)

    assert main(["forecast", "WBTC", "--posterior", "--draws", "800", "--seed", "3"]) == 0

    out = capsys.readouterr().out
    assert "R-hat" in out and "ESS" in out
    assert "point-estimate interval" in out and "posterior interval" in out
    assert "seed 3" in out
    assert "Converged" in out


def test_an_unconverged_posterior_exits_non_zero(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unconverged sampler does not announce itself, so the refusal has
    to be the exit code and not only prose someone might skim past."""
    simulate_env(tokens_file, monkeypatch)

    assert main(["forecast", "WBTC", "--posterior", "--draws", "5", "--chains", "2"]) == 1

    assert "REFUSED" in capsys.readouterr().out


def test_forecast_without_posterior_still_runs_arima(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default behaviour must not have moved."""
    simulate_env(tokens_file, monkeypatch)

    assert main(["forecast", "WBTC"]) == 0

    assert "ARIMA" in capsys.readouterr().out


def test_every_analytics_command_has_a_make_target() -> None:
    """The gap that sent a reader to `docker compose exec` by hand.

    The Makefile is the single source of commands (AGENTS.md), so a CLI
    subcommand a person would run interactively needs a target behind it.
    Services and namespaces are excluded: nobody runs `make serve` to get
    a foreground API inside the stack that is already running one.
    """
    import re
    from pathlib import Path as _Path

    makefile = _Path("Makefile").read_text()
    targets = set(re.findall(r"^([a-z][a-z0-9-]*):", makefile, re.M))
    subcommands = set(re.findall(r'add_parser\(\s*"([a-z-]+)"', _Path("candlegraph/cli.py").read_text()))
    services = {"serve", "worker", "agents", "knowledge"}

    assert (subcommands - services) <= targets, (
        f"no make target for: {sorted(subcommands - services - targets)}"
    )
