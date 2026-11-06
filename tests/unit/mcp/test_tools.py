"""The tools themselves, called directly against ``FakeDatabase``.

No protocol here on purpose (``docs/knowledge/mcp/DESIGN.md``: *"Unit tests
call the tool functions directly with FakeDatabase"*). Two of these are the
step's exit criterion written as tests: ``data_quality_report`` returns what
``report --json`` prints, and no tool in the listing writes market data.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from candlegraph.cli import main
from candlegraph.mcp import tools
from candlegraph.mcp.tools import ToolError
from candlegraph.storage import set_db
from tests.conftest import FakeDatabase, make_settings

HOUR = 3600
BASE = 1_700_000_000 // (24 * HOUR) * (24 * HOUR)


def price_rows(symbol: str, n: int, seed: int = 7) -> list[dict[str, Any]]:
    """A deterministic random walk, shaped like rows from the price query."""
    rng = np.random.default_rng(seed)
    walk = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.01, n)))
    return [
        {
            "symbol": symbol,
            "token_id": f"0x{symbol.lower()}",
            "period_start_unix": BASE + i * HOUR,
            "timestamp": datetime.fromtimestamp(BASE + i * HOUR, tz=UTC),
            "open": Decimal(str(price)),
            "high": Decimal(str(price)),
            "low": Decimal(str(price)),
            "close": Decimal(str(price)),
            "price_usd": Decimal(str(price)),
            "volume_usd": Decimal("1000"),
            "total_value_locked_usd": Decimal("5000"),
        }
        for i, price in enumerate(walk)
    ]


def coverage_row(symbol: str, stored: int, expected: int) -> dict[str, Any]:
    return {
        "token_id": f"0x{symbol.lower()}",
        "symbol": symbol,
        "stored_hours": stored,
        "expected_hours": expected,
        "first_unix": BASE,
        "last_unix": BASE + (expected - 1) * HOUR,
    }


def quality_reads() -> dict[str, list[dict[str, Any]]]:
    return {
        "coverage_report": [coverage_row("WBTC", 48, 48), coverage_row("SHIB", 12, 48)],
        "missing_hours": [{"period_start_unix": BASE + i * HOUR} for i in range(12, 48)],
        "ohlc_violations": [],
        "field_completeness": [
            {
                "rows_total": 60,
                "volume_present": 60,
                "volume_usd_present": 60,
                "tvl_usd_present": 55,
                "fees_usd_present": 60,
            }
        ],
    }


@pytest.fixture
def wired(tokens_file: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDatabase:
    """Tools pointed at a two-token registry and a database of canned reads."""
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    # `fake` is the deterministic HashEmbedder: the unit tier never downloads
    # a model and never reaches the ollama service (invariant 2).
    settings = make_settings(
        tokens_file=str(tokens_file), analytics_lookback_days=30, embedding_provider="fake"
    )
    monkeypatch.setattr(tools, "get_settings", lambda: settings)
    monkeypatch.setattr("candlegraph.cli.get_settings", lambda: settings)
    db = FakeDatabase(
        get_price_history=price_rows("WBTC", 300) + price_rows("SHIB", 300, seed=8),
        **quality_reads(),
    )
    set_db(db)
    return db


# --- the exit criterion ------------------------------------------------------


def test_the_data_quality_tool_returns_what_report_json_prints(
    wired: FakeDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    """``docs/knowledge/mcp/DESIGN.md``'s exit criterion, as one assertion.

    Both go through ``quality.report_dict``; this is what stops a second
    shape appearing the next time either surface grows a field.
    """
    from_tool = tools.data_quality_report()

    assert main(["report", "--json", "--strict"]) == 1  # SHIB is under covered
    from_cli = json.loads(capsys.readouterr().out)

    # Only the clock differs between two runs of the same read.
    from_tool.pop("generated_at")
    from_cli.pop("generated_at")
    assert from_tool == from_cli
    assert from_cli["under_covered"] == ["SHIB"]


def test_no_read_only_tool_writes_anything(wired: FakeDatabase) -> None:
    """The listing's promise: the read-only tools only ever ``SELECT``.

    Driven off the registry rather than a hand-kept list, so a tool added
    without thinking about it is caught here.
    """
    callers: dict[str, dict[str, Any]] = {
        "status": {},
        "recent_ingest_runs": {},
        "data_quality_report": {},
        "candles": {"symbol": "WBTC"},
        "stats": {"symbol": "WBTC", "window": 24},
        "compare": {"symbol_a": "WBTC", "symbol_b": "SHIB"},
        "signals": {"symbol": "WBTC"},
        "backtest": {"symbol": "WBTC"},
        "agent_runs": {},
        "latest_agent_report": {},
        "search_knowledge": {"query": "unlock"},
        "knowledge_budget": {},
    }
    read_only = [spec for spec in tools.TOOLS if spec.read_only]
    assert set(callers) == {spec.name for spec in read_only}, "a read-only tool is not exercised here"

    for spec in read_only:
        spec.run(**callers[spec.name])

    assert wired.executed == []
    assert wired.upserts == []


def test_the_two_writing_tools_say_what_they_write() -> None:
    writers = {spec.name: spec.writes for spec in tools.TOOLS if not spec.read_only}

    assert writers == {
        "observe": "agent_run",
        "explain_signal": "explanation, and document/source_call when search is true",
    }
    assert all("token_hours_data" not in writes for writes in writers.values())


# --- operating the stack -------------------------------------------------------


def test_status_reports_freshness_from_the_same_read_the_endpoint_uses(wired: FakeDatabase) -> None:
    wired.reads["get_last_successful_run"] = [{"finished_at": datetime.now(tz=UTC)}]

    body = tools.status()

    assert body["status"] == "ok"
    assert body["database"] == "up"
    assert body["data_fresh"] is True
    assert [label for label, _ in wired.fetches] == ["get_last_successful_run"]


def test_recent_ingest_runs_keeps_a_jsonb_error_map_as_data(wired: FakeDatabase) -> None:
    wired.reads["get_recent_runs"] = [
        {
            "run_id": "r-1",
            "trigger": "scheduled",
            "status": "partial",
            "started_at": datetime(2026, 11, 4, 12, tzinfo=UTC),
            "finished_at": datetime(2026, 11, 4, 12, 0, 3, tzinfo=UTC),
            "duration_seconds": Decimal("3.0"),
            "token_hours_written": 30,
            "pool_hours_written": 90,
            "errors": {"GNO": "subgraph timeout"},
        }
    ]

    payload = tools.recent_ingest_runs(limit=5)

    assert payload["count"] == 1
    assert payload["runs"][0]["errors"] == {"GNO": "subgraph timeout"}
    assert payload["runs"][0]["duration_seconds"] == 3.0
    assert payload["runs"][0]["started_at"] == "2026-11-04T12:00:00+00:00"


def test_a_limit_outside_the_cap_is_refused_rather_than_silently_clamped(wired: FakeDatabase) -> None:
    with pytest.raises(ToolError, match="between 1 and 500"):
        tools.recent_ingest_runs(limit=0)
    with pytest.raises(ToolError, match="between 1 and 500"):
        tools.agent_runs(limit=10_000)


def test_an_error_crossing_the_boundary_is_redacted(wired: FakeDatabase) -> None:
    def explode(*_: Any, **__: Any) -> list[dict[str, Any]]:
        raise RuntimeError("connection to postgresql://candlegraph:hunter2@db refused")

    wired.fetch_all = explode  # type: ignore[method-assign]

    with pytest.raises(ToolError) as caught:
        tools.recent_ingest_runs()

    assert "hunter2" not in str(caught.value)
    assert "***" in str(caught.value)


# --- market data ---------------------------------------------------------------


def test_candles_bounds_the_query_by_the_window_it_was_given(wired: FakeDatabase) -> None:
    wired.reads["chart_query"] = [
        {
            "interval_start": datetime(2026, 11, 4, tzinfo=UTC),
            "opens": [Decimal("10")],
            "closes": [Decimal("11")],
            "max_high": Decimal("12"),
            "min_low": Decimal("9"),
            "avg_price_usd": Decimal("10.5"),
        }
    ]

    payload = tools.candles("wbtc", hours=24, start="2026-11-01T00:00:00+00:00")

    assert payload["symbol"] == "WBTC"
    assert payload["interval_hours"] == 24
    assert payload["candles"] == [
        {
            "time": "2026-11-04T00:00:00",
            "open": 10.0,
            "high": 12.0,
            "low": 9.0,
            "close": 11.0,
            "price_usd": 10.5,
        }
    ]
    params = dict(next(p for label, p in wired.fetches if label == "chart_query") or {})
    assert params["start_unix"] == int(datetime(2026, 11, 1, tzinfo=UTC).timestamp())
    assert params["end_unix"] is None


def test_an_unknown_symbol_is_refused_with_the_tracked_ones_listed(wired: FakeDatabase) -> None:
    with pytest.raises(ToolError, match="unknown token 'DOGE'; tracked: WBTC, SHIB"):
        tools.candles("DOGE")
    with pytest.raises(ToolError, match="unknown token"):
        tools.stats("DOGE")


def test_a_malformed_timestamp_names_the_argument_and_the_format(wired: FakeDatabase) -> None:
    with pytest.raises(ToolError, match="`start` must be ISO 8601"):
        tools.candles("WBTC", start="last tuesday")


def test_stats_reports_the_rolling_window_it_actually_used(wired: FakeDatabase) -> None:
    payload = tools.stats("WBTC", window=24)

    assert payload["symbol"] == "WBTC"
    assert payload["window"] == 24
    assert payload["annualised_volatility"] is not None
    assert payload["bars"] == 300


def test_compare_is_computed_over_aligned_bars_and_says_so(wired: FakeDatabase) -> None:
    payload = tools.compare("WBTC", "SHIB")

    assert payload["symbol_a"] == "WBTC"
    assert payload["aligned_bars"] > 0
    assert "nothing is filled" in payload["note"]


def test_comparing_a_token_with_itself_is_refused(wired: FakeDatabase) -> None:
    with pytest.raises(ToolError, match="two different tokens"):
        tools.compare("WBTC", "wbtc")


# --- strategies -----------------------------------------------------------------


def test_signals_returns_positions_without_storing_any(wired: FakeDatabase) -> None:
    payload = tools.signals("WBTC", strategy="bollinger", tail=5)

    assert payload["strategy"] == "bollinger"
    assert payload["latest_position"] in (-1, 0, 1)
    assert payload["latest_position_label"] in ("short", "flat", "long")
    assert len(payload["bars_tail"]) == 5
    assert wired.upserts == []


def test_an_unknown_strategy_lists_the_registered_ones(wired: FakeDatabase) -> None:
    with pytest.raises(ToolError, match="unknown strategy 'momentum'; available:"):
        tools.signals("WBTC", strategy="momentum")


def test_a_backtest_reports_excess_against_buy_and_hold(wired: FakeDatabase) -> None:
    payload = tools.backtest("WBTC", strategy="bollinger", fee_bps=0.0, slippage_bps=0.0)

    result = payload["backtests"][0]
    assert result["fee_bps"] == 0.0
    assert result["excess_return"] == pytest.approx(result["total_return"] - result["buy_and_hold_return"])
    assert "buy_and_hold_return" in result["note"]


def test_backtest_all_runs_every_registered_strategy(wired: FakeDatabase) -> None:
    from candlegraph.strategies import available

    payload = tools.backtest("WBTC", strategy="all")

    assert {entry["strategy"] for entry in payload["backtests"]} == set(available())


# --- the agents --------------------------------------------------------------------


def test_the_latest_agent_report_says_so_when_there_is_none(wired: FakeDatabase) -> None:
    payload = tools.latest_agent_report("observer")

    assert payload["found"] is False
    assert "no observer run" in payload["note"]


def test_the_latest_agent_report_carries_findings_and_markdown(wired: FakeDatabase) -> None:
    wired.reads["get_recent_agent_runs"] = [
        {
            "run_id": "a-1",
            "agent": "observer",
            "status": "warning",
            "started_at": datetime(2026, 11, 4, tzinfo=UTC),
            "finished_at": datetime(2026, 11, 4, 0, 0, 2, tzinfo=UTC),
            "duration_seconds": 2.0,
            "findings_total": 1,
            "summary": "WETH: 7h gap",
            "report": "# observer — WARNING",
            "findings": [{"kind": "gap", "severity": "warning", "subject": "WETH", "message": "7h gap"}],
        }
    ]

    payload = tools.latest_agent_report()

    assert payload["found"] is True
    assert payload["markdown"] == "# observer — WARNING"
    assert payload["findings"][0]["kind"] == "gap"
    assert payload["run"]["status"] == "warning"


# --- the knowledge layer -------------------------------------------------------------


def test_search_falls_back_to_the_lexical_leg_and_says_so(
    wired: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No reachable embedder is a degraded answer, not a failure."""
    from candlegraph.knowledge import embed

    def refuse(*_: Any, **__: Any) -> Any:
        raise embed.EmbeddingError("ollama is not reachable")

    monkeypatch.setattr(embed, "build_embedder", refuse)
    wired.reads["lexical_search"] = [
        {
            "id": 1,
            "document_id": 2,
            "ordinal": 0,
            "text": "WBTC unlock scheduled",
            "url": "https://example.com/a",
            "title": "Unlock",
            "published_at": datetime(2026, 11, 3, tzinfo=UTC),
            "retrieved_at": datetime(2026, 11, 4, tzinfo=UTC),
            "score": 0.5,
            "dense_rank": None,
            "lexical_rank": 1,
        }
    ]

    payload = tools.search_knowledge("unlock", symbol="WBTC", before="2026-11-04T00:00:00+00:00")

    assert payload["dense_used"] is False
    assert payload["chunks"][0]["url"] == "https://example.com/a"
    assert payload["chunks"][0]["legs"] == "lexical"
    assert payload["bar"] == "2026-11-04T00:00:00+00:00"


def test_search_never_reaches_for_the_news_source(wired: FakeDatabase) -> None:
    """Retrieval is free; only ``explain_signal`` can spend (the design's rule)."""
    tools.search_knowledge("unlock")

    assert [label for label, _ in wired.fetches] == ["hybrid_search"] or [
        label for label, _ in wired.fetches
    ] == ["lexical_search"]
    assert not any("source_call" in label for label, _ in wired.executed)


def test_an_unbuilt_retrieval_backend_is_refused_with_the_evaluation_named(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    monkeypatch.setattr(
        tools,
        "get_settings",
        lambda: make_settings(tokens_file=str(tokens_file), retrieval_backend="ragflow"),
    )
    set_db(FakeDatabase())

    with pytest.raises(ToolError, match=r"ragflow/DESIGN\.md"):
        tools.search_knowledge("unlock")


def test_the_budget_tool_grades_spend_the_way_the_observer_does(
    tokens_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    monkeypatch.setattr(
        tools,
        "get_settings",
        lambda: make_settings(
            tokens_file=str(tokens_file), tavily_monthly_credits=1000, tavily_daily_credits=40
        ),
    )
    set_db(FakeDatabase(source_spend=[{"month": 850, "today": 4}]))

    payload = tools.knowledge_budget()

    assert payload["month_spent"] == 850
    assert payload["month_fraction"] == pytest.approx(0.85)
    assert payload["level"] == "warning"  # the guard's own 80% threshold
    assert payload["key_configured"] is False
    assert payload["usable_monthly"] == 950


# --- the two tools that write --------------------------------------------------------


def test_observe_writes_one_agent_run_row_and_nothing_else(
    wired: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its whole write surface, asserted: the ledger row an agent run *is*."""
    monkeypatch.setattr(
        tools, "get_settings", lambda: make_settings(agent_report_sinks="ledger", observer_interval=0)
    )
    wired.reads["get_last_successful_run"] = [{"finished_at": datetime.now(tz=UTC)}]

    payload = tools.observe()

    assert payload["agent"] == "observer"
    assert payload["status"] in ("ok", "info", "warning", "critical", "error")
    assert payload["markdown"].startswith("# observer")
    assert [label for label, _ in wired.executed] == ["insert_agent_run"]
    assert wired.upserts == []


def test_explain_signal_writes_the_explanation_and_does_not_search_by_default(
    wired: FakeDatabase,
) -> None:
    """`search` defaults false, so no caller spends a credit by accident."""
    # `explain` embeds pending chunks before retrieving, so the backlog read
    # has to answer; an empty corpus has nothing to embed.
    wired.reads["count_embedding_backlog"] = [{"missing": 0, "stale": 0, "total": 0}]

    payload = tools.explain_signal("WBTC", strategy="bollinger")

    assert payload["symbol"] == "WBTC"
    assert payload["news_status"] == "skipped"
    assert payload["position_label"] in ("short", "flat", "long")
    assert "[" not in payload["rationale"] or payload["citations"]
    # One write: the upsert on the signal's natural key. No source_call row,
    # because nothing was fetched.
    assert [label for label, _ in wired.executed] == ["upsert_explanation"]


def test_explaining_an_untracked_symbol_is_refused_before_anything_is_written(
    wired: FakeDatabase,
) -> None:
    with pytest.raises(ToolError, match="unknown token"):
        tools.explain_signal("DOGE")

    assert wired.executed == []


def test_a_zero_width_candle_bucket_is_refused(wired: FakeDatabase) -> None:
    with pytest.raises(ToolError, match="hours must be at least 1"):
        tools.candles("WBTC", hours=0)


def test_search_refuses_a_mode_it_does_not_have(wired: FakeDatabase) -> None:
    with pytest.raises(ToolError, match="mode must be display or strategy"):
        tools.search_knowledge("unlock", mode="hindsight")
