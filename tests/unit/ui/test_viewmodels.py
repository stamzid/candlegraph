"""The UI's view-models.

All the UI's arithmetic lives here rather than in the Streamlit script, so
this is where the UI gets its coverage. Errors a user can fix must arrive as
:class:`ViewError` with a readable message, not as a traceback on screen.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
import pytest

from candlegraph.analytics.arima import ArimaConfig, ArimaForecast
from candlegraph.analytics.portfolio import PortfolioConfig
from candlegraph.analytics.risk import RiskConfig
from candlegraph.ingestion.quality import Gap
from candlegraph.strategies.backtest import BacktestConfig
from candlegraph.strategies.bollinger import BollingerConfig
from candlegraph.ui.viewmodels import (
    Preferences,
    ViewError,
    available_symbols,
    build_agents_view,
    build_forecast,
    build_market_view,
    build_method_scores,
    build_news_view,
    build_pair_view,
    build_portfolio_view,
    build_quality_view,
    build_risk_view,
    build_strategy_view,
    build_token_view,
    candle_frame,
    completeness_frame,
    default_index,
    default_max_weight,
    default_portfolio_symbols,
    fill_overlay,
    forecast_frame,
    gap_lane_segments,
    load_prices,
    longest_gaps,
    method_catalogue,
    observed_gaps,
    range_lane_segments,
    run_backfill,
    run_fill_refresh_now,
    run_news_fetch_now,
    run_observer_now,
)
from tests.conftest import FakeDatabase, make_settings

HOUR = 3600
BASE = 1_700_000_000 // (24 * HOUR) * (24 * HOUR)
FREE = BacktestConfig(fee_bps=0.0, slippage_bps=0.0)


def price_rows(symbol: str, n: int, seed: int = 3) -> list[dict[str, Any]]:
    rng = np.random.default_rng(seed)
    walk = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.01, n)))
    return [
        {
            "symbol": symbol,
            "token_id": f"0x{symbol.lower()}",
            "period_start_unix": BASE + i * HOUR,
            "timestamp": datetime.fromtimestamp(BASE + i * HOUR, tz=UTC),
            "open": Decimal(str(p)),
            "high": Decimal(str(p * 1.01)),
            "low": Decimal(str(p * 0.99)),
            "close": Decimal(str(p)),
            "price_usd": Decimal(str(p)),
            "volume_usd": Decimal("1000"),
            "total_value_locked_usd": Decimal("5000"),
        }
        for i, p in enumerate(walk)
    ]


def _utc(unix: int) -> datetime:
    return datetime.fromtimestamp(unix, tz=UTC)


def coverage_row(symbol: str, stored: int = 400, expected: int = 400) -> dict[str, Any]:
    return {
        "token_id": f"0x{symbol.lower()}",
        "symbol": symbol,
        "stored_hours": stored,
        "first_unix": BASE,
        "last_unix": BASE + (expected - 1) * HOUR,
        "expected_hours": expected,
    }


@pytest.fixture
def db() -> FakeDatabase:
    return FakeDatabase(
        get_price_history=price_rows("WETH", 400) + price_rows("WBTC", 400, seed=4),
        coverage_report=[coverage_row("WETH"), coverage_row("WBTC")],
    )


# --- symbol discovery ------------------------------------------------------


def test_symbols_come_from_what_is_actually_stored(db: FakeDatabase) -> None:
    """A token configured an hour ago has nothing to plot yet."""
    assert available_symbols(db) == ["WBTC", "WETH"]


def test_no_stored_data_means_no_symbols() -> None:
    assert available_symbols(FakeDatabase()) == []


# --- loading ---------------------------------------------------------------


def test_choosing_no_token_is_a_readable_error(db: FakeDatabase) -> None:
    with pytest.raises(ViewError, match="at least one token"):
        load_prices(db, [], days=30)


def test_an_empty_window_is_a_readable_error() -> None:
    with pytest.raises(ViewError, match="No stored price history"):
        load_prices(FakeDatabase(), ["WETH"], days=30)


# --- the market view ---------------------------------------------------------


def test_the_market_view_rebases_every_token_to_100(db: FakeDatabase) -> None:
    view = build_market_view(db, ["WETH", "WBTC"], days=30)

    first = view.rebased.apply(lambda col: col.dropna().iloc[0])
    assert first.to_numpy() == pytest.approx([100.0, 100.0])
    assert set(view.rebased.columns) == {"WETH", "WBTC"}


def test_the_snapshot_carries_one_row_per_token_with_the_headline_columns(db: FakeDatabase) -> None:
    snapshot = build_market_view(db, ["WETH", "WBTC"], days=30).snapshot

    assert set(snapshot.index) == {"WETH", "WBTC"}
    assert {"last", "change_24h", "change_7d", "volatility", "max_drawdown", "volume_24h"} <= set(
        snapshot.columns
    )
    assert snapshot["volume_24h"].gt(0).all()


def test_the_snapshot_sorts_by_the_24h_move(db: FakeDatabase) -> None:
    snapshot = build_market_view(db, ["WETH", "WBTC"], days=30).snapshot

    changes = snapshot["change_24h"].dropna()
    assert list(changes) == sorted(changes, reverse=True)


def test_a_change_without_enough_history_is_nan_not_zero() -> None:
    """A 30-day change over a 100-bar history is undefined, not 0% (invariant 11's shape)."""
    db = FakeDatabase(get_price_history=price_rows("WETH", 100))

    snapshot = build_market_view(db, ["WETH"], days=30).snapshot

    assert np.isnan(snapshot.loc["WETH", "change_30d"])
    assert np.isfinite(snapshot.loc["WETH", "change_24h"])


def test_the_correlation_matrix_is_symmetric_with_a_unit_diagonal(db: FakeDatabase) -> None:
    corr = build_market_view(db, ["WETH", "WBTC"], days=30).correlation

    assert corr.loc["WETH", "WETH"] == pytest.approx(1.0)
    assert corr.loc["WETH", "WBTC"] == pytest.approx(corr.loc["WBTC", "WETH"])


def test_the_market_view_with_nothing_stored_is_a_readable_error() -> None:
    with pytest.raises(ViewError, match="No stored price history"):
        build_market_view(FakeDatabase(), ["WETH"], days=30)


# --- the token view ---------------------------------------------------------


def test_the_token_view_carries_candles_bands_and_summary(db: FakeDatabase) -> None:
    view = build_token_view(db, "WETH", days=30, window=24)

    assert view.symbol == "WETH"
    assert view.bars == 400
    assert {"open", "high", "low", "close", "volume_usd"} <= set(view.ohlc.columns)
    assert {"price", "rolling_mean", "rolling_volatility"} <= set(view.summary.columns)
    assert {"middle", "upper", "lower", "zscore"} <= set(view.bands.columns)
    assert (view.drawdown <= 0).all()


def test_the_latest_strip_skips_incomplete_windows_and_prices_the_tail(db: FakeDatabase) -> None:
    latest = build_token_view(db, "WETH", days=30, window=24).latest

    assert np.isfinite(latest["rolling_mean"])
    assert np.isfinite(latest["rolling_volatility"])
    assert np.isfinite(latest["change_24h"])
    assert latest["var_1d"] > 0


def test_too_few_bars_for_a_tail_leaves_var_nan_rather_than_guessing() -> None:
    db = FakeDatabase(get_price_history=price_rows("WETH", 25))

    view = build_token_view(db, "WETH", days=30, window=10)

    assert np.isnan(view.var_1d)


def test_a_window_longer_than_the_history_says_so(db: FakeDatabase) -> None:
    with pytest.raises(ViewError, match="fewer than the 500-bar window"):
        build_token_view(db, "WETH", days=30, window=500)


def test_an_unknown_token_is_a_readable_error(db: FakeDatabase) -> None:
    with pytest.raises(ViewError, match="No stored price history for DOGE"):
        build_token_view(db, "DOGE", days=30, window=24)


def test_band_width_is_configurable(db: FakeDatabase) -> None:
    narrow = build_token_view(db, "WETH", 30, 24, num_std=1.0).bands.dropna().iloc[-1]
    wide = build_token_view(db, "WETH", 30, 24, num_std=3.0).bands.dropna().iloc[-1]

    assert wide["upper"] > narrow["upper"]


def test_short_histories_are_not_resampled(db: FakeDatabase) -> None:
    ohlc = build_token_view(db, "WETH", 30, 24).ohlc

    assert candle_frame(ohlc, target_bars=500).equals(ohlc)


def test_resampled_candles_keep_the_true_extremes_and_total_volume(db: FakeDatabase) -> None:
    """Display resampling must not invent or lose a high, a low, or volume."""
    ohlc = build_token_view(db, "WETH", 30, 24).ohlc

    candles = candle_frame(ohlc, target_bars=100)

    assert len(candles) <= 101
    assert candles["high"].max() == pytest.approx(ohlc["high"].max())
    assert candles["low"].min() == pytest.approx(ohlc["low"].min())
    assert candles["volume_usd"].sum() == pytest.approx(ohlc["volume_usd"].sum())
    assert candles["open"].iloc[0] == pytest.approx(ohlc["open"].iloc[0])
    assert candles["close"].iloc[-1] == pytest.approx(ohlc["close"].iloc[-1])


# --- the risk view -----------------------------------------------------------


def test_the_risk_view_reports_and_compares_every_estimator(db: FakeDatabase) -> None:
    view = build_risk_view(db, "WETH", "WBTC", 30, RiskConfig(confidence=0.95, horizon_bars=24))

    assert view.report.symbol == "WETH"
    assert view.report.decomposition is not None
    assert view.report.decomposition.benchmark == "WBTC"
    assert set(view.estimators.index) == {"historical", "parametric", "cornish_fisher"}
    assert (view.estimators["CVaR"] >= view.estimators["VaR"]).all()
    assert (view.drawdown <= 0).all()


def test_a_token_is_never_its_own_benchmark(db: FakeDatabase) -> None:
    """Regressing a token on itself is a tautology; the split is simply omitted."""
    view = build_risk_view(db, "WETH", "WETH", 30, RiskConfig())

    assert view.report.decomposition is None


def test_too_little_history_for_a_tail_is_a_readable_error() -> None:
    db = FakeDatabase(get_price_history=price_rows("WETH", 20))

    with pytest.raises(ViewError, match="need at least"):
        build_risk_view(db, "WETH", None, 30, RiskConfig())


# --- the pair view -----------------------------------------------------------


def test_the_pair_view_compares_and_shapes_the_beta_cloud(db: FakeDatabase) -> None:
    view = build_pair_view(db, "WETH", "WBTC", 30, window=24)

    assert view.comparison.bars == 400
    assert list(view.scatter.columns) == ["WBTC", "WETH"]
    line = view.beta_line
    slope = (line["y"].iloc[1] - line["y"].iloc[0]) / (line["x"].iloc[1] - line["x"].iloc[0])
    assert slope == pytest.approx(view.comparison.beta)


def test_comparing_a_token_with_itself_is_a_readable_error(db: FakeDatabase) -> None:
    with pytest.raises(ViewError, match="two different tokens"):
        build_pair_view(db, "WETH", "WETH", 30, window=24)


def test_a_missing_leg_is_a_readable_error(db: FakeDatabase) -> None:
    with pytest.raises(ViewError, match="DOGE"):
        build_pair_view(db, "WETH", "DOGE", 30, window=24)


# --- the portfolio view ----------------------------------------------------


def test_a_portfolio_needs_two_tokens(db: FakeDatabase) -> None:
    with pytest.raises(ViewError, match="at least two tokens"):
        build_portfolio_view(db, ["WETH"], 30, PortfolioConfig())


def test_the_portfolio_view_solves_and_counts_bars(db: FakeDatabase) -> None:
    view = build_portfolio_view(db, ["WETH", "WBTC"], 30, PortfolioConfig())

    assert set(view.result.weights) == {"WETH", "WBTC"}
    assert view.bars > 0
    assert view.symbols == ("WBTC", "WETH")


def test_the_weights_frame_drops_zero_positions_and_sorts(db: FakeDatabase) -> None:
    view = build_portfolio_view(db, ["WETH", "WBTC"], 30, PortfolioConfig(objective="min_volatility"))
    frame = view.weights_frame

    assert list(frame["weight"]) == sorted(frame["weight"], reverse=True)
    assert (frame["weight"].abs() > 1e-6).all()


def test_the_risk_budget_shares_sum_to_one_for_the_held_assets(db: FakeDatabase) -> None:
    view = build_portfolio_view(db, ["WETH", "WBTC"], 30, PortfolioConfig(objective="equal_weight"))

    budget = view.risk_contributions
    assert set(budget.index) <= {"WETH", "WBTC"}
    assert float(budget["risk_share"].sum()) == pytest.approx(1.0)


def test_tail_risk_is_measured_on_the_solved_allocation(db: FakeDatabase) -> None:
    view = build_portfolio_view(
        db, ["WETH", "WBTC"], 30, PortfolioConfig(objective="equal_weight"), risk=RiskConfig()
    )

    assert view.var > 0
    assert view.cvar >= view.var


def test_tail_risk_is_nan_when_not_asked_for(db: FakeDatabase) -> None:
    view = build_portfolio_view(db, ["WETH", "WBTC"], 30, PortfolioConfig())

    assert np.isnan(view.var)
    assert np.isnan(view.cvar)


def test_no_frontier_is_traced_by_default(db: FakeDatabase) -> None:
    view = build_portfolio_view(db, ["WETH", "WBTC"], 30, PortfolioConfig())

    assert view.frontier == []
    assert view.frontier_frame.empty


def test_a_frontier_is_traced_when_asked(db: FakeDatabase) -> None:
    view = build_portfolio_view(db, ["WETH", "WBTC"], 30, PortfolioConfig(), frontier_points=6)

    assert len(view.frontier) >= 2
    assert {"volatility", "expected_return", "sharpe"} == set(view.frontier_frame.columns)


def test_an_infeasible_cap_becomes_a_readable_error(db: FakeDatabase) -> None:
    """A PortfolioError must not reach the screen as a traceback."""
    with pytest.raises(ViewError, match="cannot sum to 1"):
        build_portfolio_view(db, ["WETH", "WBTC"], 30, PortfolioConfig(max_weight=0.2))


def test_the_risk_free_rate_reaches_the_result(db: FakeDatabase) -> None:
    view = build_portfolio_view(db, ["WETH", "WBTC"], 30, PortfolioConfig(risk_free_rate=0.05))

    assert view.result.risk_free_rate == 0.05


# --- the strategy view -----------------------------------------------------


def test_the_strategy_view_runs_and_backtests(db: FakeDatabase) -> None:
    view = build_strategy_view(db, "WETH", "bollinger", 30, BollingerConfig(window=20), FREE)

    assert view.strategy.name == "bollinger"
    assert view.report.bars == 400
    assert view.report.symbol == "WETH"


def test_the_equity_frame_compares_against_buy_and_hold(db: FakeDatabase) -> None:
    view = build_strategy_view(db, "WETH", "bollinger", 30, None, FREE)
    frame = view.equity_frame

    assert list(frame.columns) == ["strategy", "buy and hold"]
    # Both start from the same capital, so the comparison is fair.
    assert frame["strategy"].iloc[0] == pytest.approx(frame["buy and hold"].iloc[0], rel=1e-6)


def test_the_trade_ledger_lists_every_position_change(db: FakeDatabase) -> None:
    view = build_strategy_view(db, "WETH", "bollinger", 30, None, FREE)

    trades = view.trades_frame
    assert list(trades.columns) == ["from", "to", "price"]
    assert len(trades) == view.strategy.trades()
    assert (trades["from"] != trades["to"]).all()


def test_each_registered_strategy_renders(db: FakeDatabase) -> None:
    for name in ("bollinger", "crossover"):
        view = build_strategy_view(db, "WETH", name, 30, None, FREE)
        assert view.report.bars == 400


def test_an_unknown_token_for_a_strategy_is_readable(db: FakeDatabase) -> None:
    with pytest.raises(ViewError, match="No stored price history for DOGE"):
        build_strategy_view(db, "DOGE", "bollinger", 30, None, FREE)


def test_strategy_config_reaches_the_run(db: FakeDatabase) -> None:
    view = build_strategy_view(db, "WETH", "bollinger", 30, BollingerConfig(window=40, entry_z=3.0), FREE)

    assert view.strategy.params["window"] == 40
    assert view.strategy.params["entry_z"] == 3.0


# --- the forecast view -----------------------------------------------------


def test_the_forecast_view_returns_prices_and_a_fit(db: FakeDatabase) -> None:
    prices, forecast = build_forecast(db, "WETH", 30, ArimaConfig())

    assert len(prices) == 400
    assert forecast.ok
    assert np.isfinite(forecast.predicted)


def test_a_forecast_for_an_unknown_token_is_readable(db: FakeDatabase) -> None:
    with pytest.raises(ViewError, match="No stored price history for DOGE"):
        build_forecast(db, "DOGE", 30, ArimaConfig())


def test_the_forecast_frame_continues_past_the_history(db: FakeDatabase) -> None:
    """Plotting the forecast on top of the history would hide it."""
    prices, forecast = build_forecast(db, "WETH", 30, ArimaConfig(forecast_steps=6))

    frame = forecast_frame(prices, forecast)

    assert len(frame) == 6
    assert frame.index[0] > pd.DatetimeIndex(prices.index)[-1]
    assert (frame.index.to_series().diff().dropna() == pd.Timedelta(hours=1)).all()


def test_a_failed_forecast_yields_an_empty_frame_not_a_crash() -> None:
    prices = pd.Series([1.0], index=pd.date_range("2026-01-01", periods=1, freq="h", tz="UTC"))

    frame = forecast_frame(prices, ArimaForecast(ok=False, order=(1, 1, 1)))

    assert frame.empty
    assert list(frame.columns) == ["forecast"]


def test_too_little_history_fails_softly_rather_than_raising() -> None:
    db = FakeDatabase(get_price_history=price_rows("WETH", 20))

    _, forecast = build_forecast(db, "WETH", 30, ArimaConfig(min_observations=60))

    assert forecast.ok is False
    assert forecast.message


# --- the agents view ---------------------------------------------------------


def agent_run_row(status: str = "warning", agent: str = "observer") -> dict[str, Any]:
    return {
        "run_id": "r-1",
        "agent": agent,
        "status": status,
        "started_at": datetime(2026, 10, 30, 12, 0, tzinfo=UTC),
        "finished_at": datetime(2026, 10, 30, 12, 0, 5, tzinfo=UTC),
        "duration_seconds": 5.0,
        "findings_total": 2,
        "summary": "WETH: 7h gap on a liquid token",
        "report": "# observer",
        "findings": [
            {"kind": "gap", "severity": "warning", "subject": "WETH", "message": "7h gap", "evidence": {}},
            {"kind": "stale", "severity": "ok", "subject": "ingest", "message": "fresh", "evidence": {}},
        ],
        "detail": {},
    }


def ingest_run_row(written: int = 100) -> dict[str, Any]:
    return {
        "run_id": "i-1",
        "trigger": "scheduled",
        "status": "succeeded",
        "started_at": datetime(2026, 10, 30, 12, 0, tzinfo=UTC),
        "token_hours_written": written,
        "pool_hours_written": written * 3,
        "duration_seconds": 3.0,
    }


def test_the_agents_view_reads_both_ledgers_and_the_coverage() -> None:
    db = FakeDatabase(
        get_recent_agent_runs=[agent_run_row(), agent_run_row(status="ok", agent="janitor")],
        get_recent_runs=[ingest_run_row()],
        coverage_report=[coverage_row("WETH"), coverage_row("GNO", stored=190, expected=400)],
    )

    view = build_agents_view(db)

    assert view.status == "warning"
    assert len(view.agent_runs) == 2
    assert list(view.findings.columns) == ["severity", "kind", "subject", "message"]
    assert len(view.findings) == 2
    assert list(view.ingest_runs["token_hours_written"]) == [100]
    # Thinnest coverage first, so the dashboard's headline reads off row one.
    assert view.coverage.index[0] == "GNO"
    assert view.coverage.loc["GNO", "coverage"] == pytest.approx(190 / 400)


def test_the_latest_report_is_the_newest_observer_run_not_the_janitors() -> None:
    db = FakeDatabase(
        get_recent_agent_runs=[agent_run_row(status="ok", agent="janitor"), agent_run_row()],
    )

    view = build_agents_view(db)

    assert view.latest is not None
    assert view.latest["agent"] == "observer"


def test_an_empty_system_renders_calmly() -> None:
    view = build_agents_view(FakeDatabase())

    assert view.status == "none"
    assert view.latest is None
    assert view.agent_runs.empty
    assert view.findings.empty
    assert view.ingest_runs.empty
    assert view.coverage.empty


def test_run_observer_now_posts_to_the_ledger_and_returns_the_report(tokens_file: Any) -> None:
    """The on-demand run must land in the history the dashboard reads."""
    db = FakeDatabase()
    settings = make_settings(tokens_file=str(tokens_file))

    report = run_observer_now(settings, db)

    assert report.agent == "observer"
    assert "insert_agent_run" in [label for label, _ in db.executed]


# --- sensible defaults ---------------------------------------------------------------


def test_the_default_token_is_wbtc_when_stored() -> None:
    symbols = ["AAVE", "USDT", "WBTC", "WETH"]

    assert symbols[default_index(symbols)] == "WBTC"


def test_without_wbtc_the_default_is_the_first_token_that_is_not_excluded() -> None:
    assert default_index(["AAVE", "LINK"]) == 0
    assert default_index(["AAVE", "LINK"], exclude="AAVE") == 1
    assert default_index([]) == 0


def test_the_default_benchmark_is_usdt_and_never_the_token_itself() -> None:
    symbols = ["USDT", "WBTC"]

    assert symbols[default_index(symbols, "USDT", exclude="WBTC")] == "USDT"
    assert symbols[default_index(symbols, "USDT", exclude="USDT")] == "WBTC"


def test_the_default_book_is_majors_first_with_stablecoins_left_out() -> None:
    symbols = ["AAVE", "DAI", "GNO", "LINK", "SHIB", "UNI", "USDC", "USDT", "WBTC", "WETH"]

    assert default_portfolio_symbols(symbols) == ["WBTC", "WETH", "AAVE", "GNO", "LINK"]


def test_a_universe_of_only_stablecoins_still_yields_a_book() -> None:
    assert default_portfolio_symbols(["DAI", "USDC", "USDT"]) == ["DAI", "USDC", "USDT"]


@pytest.mark.parametrize(
    ("n_assets", "expected"),
    [(10, 0.25), (5, 0.25), (4, 0.25), (3, 0.35), (2, 0.50), (1, 1.0), (0, 1.0)],
)
def test_the_default_cap_is_a_quarter_unless_too_few_tokens_make_that_infeasible(
    n_assets: int, expected: float
) -> None:
    """Two tokens at 25% each cannot sum to one; the slider must open on a feasible value."""
    assert default_max_weight(n_assets) == pytest.approx(expected)


# --- the backfill guard ----------------------------------------------------------------


def test_a_backfill_of_zero_days_is_refused(tokens_file: Any) -> None:
    with pytest.raises(ViewError, match="at least one day"):
        run_backfill(make_settings(tokens_file=str(tokens_file)), FakeDatabase(), 0)


def test_a_backfill_without_an_api_key_is_a_readable_error(tokens_file: Any) -> None:
    settings = make_settings(tokens_file=str(tokens_file), graph_api_key=None)

    with pytest.raises(ViewError, match="GRAPH_API_KEY"):
        run_backfill(settings, FakeDatabase(), 30)


def test_a_backfill_reaches_the_ingest_runner_as_a_backfill_trigger(
    tokens_file: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def fake_run_ingest(db: Any, registry: Any, settings: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return "result"

    monkeypatch.setattr("candlegraph.ui.viewmodels.run_ingest", fake_run_ingest)
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))

    assert run_backfill(make_settings(tokens_file=str(tokens_file)), FakeDatabase(), 90) == "result"
    assert captured == {"trigger": "backfill", "lookback_days": 90}


# --- the news view -------------------------------------------------------------------


def source_call_row(status: str = "ok", credits: int = 1) -> dict[str, Any]:
    return {
        "called_at": datetime(2026, 11, 1, 14, 0, tzinfo=UTC),
        "status": status,
        "credits": credits,
        "results": 5,
        "query": "WBTC crypto news",
        "error": None,
    }


def news_db(**overrides: Any) -> FakeDatabase:
    reads: dict[str, Any] = {
        "source_spend": [{"month": 820, "today": 4}],
        "get_recent_source_calls": [
            source_call_row("ok"),
            source_call_row("error"),
            source_call_row("budget_refused", credits=0),
            source_call_row("ok"),
        ],
        "source_call_daily_stats": [
            {
                "day": "2026-11-01",
                "calls": 4,
                "credits": 3,
                "results": 10,
                "ok": 2,
                "errors": 1,
                "rate_limited": 0,
                "refused": 1,
            }
        ],
        "get_recent_source_documents": [
            {
                "id": 1,
                "title": "A story",
                "url": "https://x.com/a",
                "published_at": None,
                "retrieved_at": datetime(2026, 11, 1, tzinfo=UTC),
            }
        ],
        "count_source_documents": [
            {"documents": 7, "chunks": 30, "embedded": 20, "undated": 1, "last_retrieved_at": None}
        ],
        "count_embedding_backlog": [{"missing": 8, "stale": 2, "total": 30}],
    }
    reads.update(overrides)
    return FakeDatabase(**reads)


def test_the_news_view_reads_the_budget_the_ledger_and_the_corpus(tokens_file: Any) -> None:
    settings = make_settings(
        tokens_file=str(tokens_file), tavily_monthly_credits=1000, tavily_daily_credits=40
    )

    view = build_news_view(news_db(), settings, now=datetime(2026, 11, 1, 15, 0, tzinfo=UTC))

    assert (view.spent_month, view.spent_today) == (820, 4)
    assert view.monthly_usage == pytest.approx(0.82)
    assert view.level == "warning", "82% of the month is the observer's WARNING"
    assert view.daily_usage == pytest.approx(0.10)
    assert view.outcome_counts == {"ok": 2, "error": 1, "budget_refused": 1}
    assert view.last_call is not None and view.last_call["status"] == "ok"
    assert view.corpus["documents"] == 7
    assert view.embedding_pending == 10
    assert list(view.documents.columns) == ["retrieved_at", "published_at", "title", "url"]


def test_an_untouched_ledger_renders_a_calm_empty_view(tokens_file: Any) -> None:
    view = build_news_view(FakeDatabase(), make_settings(tokens_file=str(tokens_file)))

    assert (view.spent_month, view.spent_today) == (0, 0)
    assert view.level == "ok"
    assert view.last_call is None
    assert view.outcome_counts == {}
    assert view.calls.empty and view.daily.empty and view.documents.empty
    assert view.embedding_pending == 0


def test_an_exhausted_month_grades_critical(tokens_file: Any) -> None:
    db = news_db(source_spend=[{"month": 1000, "today": 0}])

    view = build_news_view(db, make_settings(tokens_file=str(tokens_file), tavily_monthly_credits=1000))

    assert view.level == "critical"


def test_run_news_fetch_now_turns_a_missing_key_into_a_readable_error(tokens_file: Any) -> None:
    settings = make_settings(tokens_file=str(tokens_file), tavily_api_key=None)

    with pytest.raises(ViewError, match="TAVILY_API_KEY"):
        run_news_fetch_now(settings, FakeDatabase())


def test_the_agents_view_reads_the_latest_runs_stages_in_order() -> None:
    row = agent_run_row()
    row["detail"] = {
        "stages": [
            {
                "name": "collect_ledger",
                "status": "ok",
                "started_at": None,
                "duration_seconds": 0.05,
                "detail": {"runs": 39},
            },
            {
                "name": "assess",
                "status": "skipped",
                "started_at": None,
                "duration_seconds": None,
                "detail": {"reason": "x"},
            },
        ]
    }
    view = build_agents_view(FakeDatabase(get_recent_agent_runs=[row]))

    assert list(view.stages["stage"]) == ["collect_ledger", "assess"]
    assert list(view.stages["status"]) == ["ok", "skipped"]
    assert view.stages["seconds"].iloc[0] == 0.05 and pd.isna(view.stages["seconds"].iloc[1])
    assert view.stages["detail"].iloc[0] == "runs=39"


def test_a_run_recorded_before_stages_existed_gives_an_empty_frame_not_an_error() -> None:
    view = build_agents_view(FakeDatabase(get_recent_agent_runs=[agent_run_row()]))

    assert view.stages.empty and list(view.stages.columns) == ["stage", "status", "seconds", "detail"]


def test_the_agents_view_summarises_the_news_runs() -> None:
    db = FakeDatabase(
        get_recent_source_calls=[source_call_row("rate_limited"), source_call_row("ok")],
    )

    view = build_agents_view(db)

    assert view.news_status == "rate_limited", "the newest call's outcome leads"
    assert view.news_counts == {"rate_limited": 1, "ok": 1}


def test_the_agents_view_without_news_calls_says_none() -> None:
    view = build_agents_view(FakeDatabase())

    assert view.news_status == "none"
    assert view.news_counts == {}


# --- explanations on the strategy dashboard ----------------------------------------


def _explanation_row() -> dict[str, Any]:
    return {
        "strategy": "bollinger",
        "symbol": "WETH",
        "period_start_unix": 1_792_850_400,
        "position": -1,
        "numeric_context": {
            "score": 2.31,
            "move_24h": 0.08,
            "reference": {"symbol": "WBTC", "move_24h": 0.004},
        },
        "citations": [
            {
                "n": 1,
                "chunk_id": 5,
                "document_id": 9,
                "url": "https://news.example/unlock",
                "title": "Unlock",
                "published_at": "2026-10-24T09:12:00+00:00",
                "retrieved_at": "2026-10-24T14:03:00+00:00",
                "quote": "WETH moved on an unlock.",
                "legs": "both",
                "score": 0.03,
            }
        ],
        "rationale": "WETH rose on an unlock [1].",
        "model": None,
        "generated_at": datetime(2026, 11, 4, 9, 30, tzinfo=UTC),
    }


def test_the_stored_explanation_view_shapes_the_headline_citations_and_status() -> None:
    from candlegraph.ui.viewmodels import load_stored_explanation

    view = load_stored_explanation(
        FakeDatabase(get_latest_explanation=[_explanation_row()]), "bollinger", "WETH"
    )

    assert view is not None
    head = view.headline
    assert head["position"] == "short" and head["score"] == 2.31 and head["move_24h"] == 0.08
    assert head["reference_symbol"] == "WBTC" and head["citations"] == 1
    frame = view.citations_frame
    assert list(frame.columns) == ["n", "published", "title", "url", "legs", "quote"]
    assert frame.iloc[0]["published"] == "2026-10-24 09:12 UTC" and frame.iloc[0]["url"].startswith(
        "https://"
    )
    assert view.status_line.startswith("news: stored")
    assert view.validator_note is None


def test_no_stored_explanation_is_none_and_an_empty_citation_frame_keeps_its_columns() -> None:
    from candlegraph.knowledge.explain import Explanation
    from candlegraph.ui.viewmodels import ExplanationView, load_stored_explanation

    assert load_stored_explanation(FakeDatabase(), "bollinger", "WETH") is None
    bare = Explanation.from_row({**_explanation_row(), "citations": [], "numeric_context": {}})
    view = ExplanationView(bare)
    assert view.citations_frame.empty and list(view.citations_frame.columns) == [
        "n",
        "published",
        "title",
        "url",
        "legs",
        "quote",
    ]
    assert view.headline["reference_symbol"] is None


def test_run_explain_now_explains_the_screen_strategy_and_spends_nothing_unless_asked(
    db: FakeDatabase, tokens_file: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from candlegraph.ui.viewmodels import run_explain_now

    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    db.reads["hybrid_search"] = []
    db.reads["lexical_search"] = []
    db.reads["count_embedding_backlog"] = [{"missing": 0, "stale": 0, "total": 0}]
    strategy = build_strategy_view(db, "WBTC", "bollinger", 30, BollingerConfig(window=20), FREE).strategy
    settings = make_settings(
        tokens_file=str(tokens_file),
        embedding_provider="fake",
        tavily_api_key="tvly-x",
        knowledge_reference_symbol="WBTC",
    )

    view = run_explain_now(settings, db, strategy, "WBTC", search=False)

    assert view.explanation.news_status == "skipped"
    assert view.explanation.numeric_context["reference"] is None, "a token is not its own market"
    assert any(label == "upsert_explanation" for label, _ in db.executed)
    assert all(label != "insert_source_call" for label, _ in db.executed)
    assert "numbers alone" in view.explanation.rationale


def test_run_explain_now_turns_a_bad_bar_into_a_view_error(
    db: FakeDatabase, tokens_file: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from candlegraph.ui.viewmodels import run_explain_now

    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    strategy = build_strategy_view(db, "WBTC", "bollinger", 30, None, FREE).strategy
    settings = make_settings(tokens_file=str(tokens_file), embedding_provider="fake")

    with pytest.raises(ViewError, match="no bollinger bar"):
        run_explain_now(settings, db, strategy, "WBTC", search=False, at=datetime(2031, 1, 1, tzinfo=UTC))
    with pytest.raises(ViewError, match="unknown token"):
        run_explain_now(settings, db, strategy, "DOGE", search=False)


# --- quality dashboard: gaps as a descriptive fact ---------------------------


def test_a_quiet_hour_inside_the_range_is_reported_as_a_gap() -> None:
    """Uniswap emits no row for an hour with no swaps, so the hour is absent."""
    hours = [BASE, BASE + HOUR, BASE + 3 * HOUR]

    gaps = observed_gaps(hours)

    assert len(gaps) == 1
    assert gaps[0].hours == 1
    assert gaps[0].start_unix == BASE + 2 * HOUR


def test_consecutive_missing_hours_collapse_into_one_gap() -> None:
    """ "3 gaps, longest 14h" is actionable; 212 missing hours is not."""
    hours = [BASE, BASE + 5 * HOUR]

    gaps = observed_gaps(hours)

    assert len(gaps) == 1
    assert gaps[0].hours == 4


def test_a_token_that_started_late_has_a_short_history_not_a_leading_gap() -> None:
    """Only the interior counts: absence before the first bar is not a hole.

    The same rule `coverage` uses — `expected_hours` spans the token's own
    stored range, not the window the user picked.
    """
    hours = [BASE + 100 * HOUR, BASE + 101 * HOUR]

    assert observed_gaps(hours) == []


def test_a_series_of_one_bar_has_no_gaps() -> None:
    assert observed_gaps([BASE]) == []
    assert observed_gaps([]) == []


def test_only_the_longest_gaps_are_shaded_and_they_stay_in_time_order() -> None:
    """Hundreds of one-hour holes render as a barcode; the long ones carry the meaning."""
    gaps = [
        Gap(BASE, BASE),
        Gap(BASE + 10 * HOUR, BASE + 19 * HOUR),
        Gap(BASE + 30 * HOUR, BASE + 30 * HOUR),
        Gap(BASE + 50 * HOUR, BASE + 54 * HOUR),
    ]

    kept = longest_gaps(gaps, 2)

    assert [g.hours for g in kept] == [10, 5], "the two longest"
    assert [g.start_unix for g in kept] == sorted(g.start_unix for g in kept), "chronological"
    assert longest_gaps(gaps, 10) == gaps, "under the cap, nothing is dropped"


def test_the_token_view_measures_coverage_over_the_plotted_range(db: FakeDatabase) -> None:
    """A reader comparing two charts deserves to know which one has holes."""
    rows = price_rows("WETH", 60)
    del rows[30:34]  # a four-hour hole in the middle
    gappy = FakeDatabase(get_price_history=rows, coverage_report=[coverage_row("WETH")])

    view = build_token_view(gappy, "WETH", None, window=5)

    assert view.bars == 56
    assert view.missing_hours == 4
    assert view.span_hours == 60
    assert view.coverage == pytest.approx(56 / 60)
    assert view.longest_gap is not None and view.longest_gap.hours == 4


# --- quality dashboard: the report, shaped for a screen ----------------------


def quality_db(stored: int = 90, expected: int = 100) -> FakeDatabase:
    """One under-covered token with a real gap, plus completeness counts.

    WBTC and SHIB are the two tokens in the `tokens_file` fixture's registry;
    storing only WBTC leaves SHIB genuinely un-ingested, which is what
    `missing_tokens` is for.
    """
    return FakeDatabase(
        coverage_report=[coverage_row("WBTC", stored=stored, expected=expected)],
        missing_hours=[{"period_start_unix": BASE + i * HOUR} for i in range(10, 20)],
        field_completeness=[
            {"rows_total": 100, "volume_present": 100, "tvl_usd_present": 40, "fees_usd_present": 0}
        ],
    )


def test_the_quality_table_agrees_with_the_report_the_cli_prints(
    tokens_file: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three surfaces read one function; a fourth shape here would be a bug.

    `candlegraph report`, the `data_quality_report` MCP tool and this screen
    all go through `quality.build_report`, so the screen's numbers are
    compared against `report_dict` rather than recomputed.
    """
    from candlegraph.ingestion.quality import report_dict

    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    db = quality_db()
    settings = make_settings(tokens_file=str(tokens_file), min_expected_coverage=0.95)

    view = build_quality_view(db, settings)
    expected = report_dict(view.report)

    row = view.tokens.iloc[0]
    printed = expected["tokens"][0]
    assert row["symbol"] == printed["symbol"]
    assert row["stored_hours"] == printed["stored_hours"]
    assert row["missing_hours"] == printed["missing_hours"]
    assert row["coverage"] == pytest.approx(printed["coverage"])
    assert row["gaps"] == printed["gaps"]
    assert row["longest_gap_hours"] == printed["longest_gap_hours"]


def test_the_quality_view_counts_gaps_and_names_the_worst(
    tokens_file: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    settings = make_settings(tokens_file=str(tokens_file), min_expected_coverage=0.95)

    view = build_quality_view(quality_db(), settings)

    assert view.headline["worst_gap_hours"] == 10
    assert view.headline["worst_gap_symbol"] == "WBTC"
    assert view.under_covered == ["WBTC"], "90 of 100 hours, under a 95% threshold"
    assert view.headline["ok"] is False
    assert list(view.gaps.columns) == ["symbol", "start", "end", "hours"]


def test_a_configured_token_with_no_rows_at_all_is_named_not_merely_thin(
    tokens_file: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Thin coverage is the market; no rows at all is an ingestion fault."""
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    settings = make_settings(tokens_file=str(tokens_file))

    view = build_quality_view(quality_db(), settings)

    assert view.missing_tokens == ["SHIB"], "configured in tokens.yaml, never ingested"


def test_completeness_reports_a_fraction_per_field_and_survives_an_empty_table() -> None:
    """A blank field is NULL — not collected — and never a real zero (invariant 11)."""
    frame = completeness_frame(
        {"rows_total": 200, "volume_present": 200, "tvl_usd_present": 50, "fees_usd_present": 0}
    )

    assert list(frame["field"]) == ["fees_usd", "tvl_usd", "volume"], "worst first"
    assert list(frame["fraction"]) == [0.0, 0.25, 1.0]
    # No rows means no denominator; the page shows "no counts", not a crash.
    assert completeness_frame({"rows_total": 0, "volume_present": 0}).empty
    assert completeness_frame({}).empty


def test_a_new_completeness_column_appears_without_an_edit() -> None:
    """Driven off the `_present` suffix, so the query can grow a column."""
    frame = completeness_frame({"rows_total": 10, "liquidity_present": 5})

    assert list(frame["field"]) == ["liquidity"]


# --- quality dashboard: the gap map ------------------------------------------


def test_gap_lanes_break_between_runs_and_cover_the_whole_missing_bucket() -> None:
    """One plotly trace for every gap: `None` is what breaks the line.

    The end coordinate is one hour past the last missing bucket, because a
    gap of one hour covers that hour rather than being a zero-width instant.
    """
    gaps = pd.DataFrame(
        [
            {"symbol": "WETH", "start": _utc(BASE), "end": _utc(BASE), "hours": 1},
            {"symbol": "SHIB", "start": _utc(BASE), "end": _utc(BASE + HOUR), "hours": 2},
        ]
    )

    xs, ys = gap_lane_segments(gaps, ["WETH", "SHIB"])

    assert ys == ["WETH", "WETH", None, "SHIB", "SHIB", None]
    assert xs[1] - xs[0] == timedelta(hours=1), "a one-hour gap spans one hour"
    assert xs[2] is None and xs[5] is None
    assert xs[4] - xs[3] == timedelta(hours=2)


def test_gap_lanes_skip_tokens_that_are_not_on_the_map() -> None:
    """The map caps its lanes; a gap on a token with no lane must not float."""
    gaps = pd.DataFrame([{"symbol": "SHIB", "start": _utc(BASE), "end": _utc(BASE), "hours": 1}])

    assert gap_lane_segments(gaps, ["WETH"]) == ([], [])
    assert gap_lane_segments(pd.DataFrame(columns=["symbol", "start", "end", "hours"]), ["WETH"]) == ([], [])


def test_a_short_history_draws_a_short_lane_rather_than_a_hole(
    tokens_file: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The background range is what keeps "late start" and "gap" apart."""
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    view = build_quality_view(quality_db(), make_settings(tokens_file=str(tokens_file)))

    xs, ys = range_lane_segments(view.tokens, ["WBTC"])

    assert ys == ["WBTC", "WBTC", None]
    assert xs[0] == view.tokens.iloc[0]["first"]
    assert xs[1] == view.tokens.iloc[0]["last"]


def test_a_token_with_no_stored_range_draws_no_lane() -> None:
    tokens = pd.DataFrame([{"symbol": "WETH", "first": None, "last": None}])

    assert range_lane_segments(tokens, ["WETH"]) == ([], [])
    assert range_lane_segments(pd.DataFrame(columns=["symbol", "first", "last"]), ["WETH"]) == ([], [])


# --- compare: the numbers the CLI prints and the screen dropped --------------


def test_the_compare_screen_shows_each_leg_on_its_own(db: FakeDatabase) -> None:
    """`candlegraph compare` prints twelve numbers; the screen showed five.

    Without the per-leg column a reader cannot see which token carried the
    return or the risk — only that they moved together.
    """
    view = build_pair_view(db, "WETH", "WBTC", None, window=24)

    legs = view.legs_frame

    assert list(legs["token"]) == ["WETH", "WBTC"]
    assert list(legs.columns) == ["token", "total_return", "volatility", "sharpe"]
    assert legs.iloc[0]["total_return"] == pytest.approx(view.comparison.total_return_a)
    assert legs.iloc[1]["sharpe"] == pytest.approx(view.comparison.sharpe_b)


# --- remembering what the user picked ---------------------------------------


def test_a_choice_survives_a_dashboard_switch() -> None:
    """The bug this exists for: pick WBTC/DAI, leave, come back to defaults.

    Streamlit drops a widget's own state as soon as a rerun does not render
    it, so the mirror is what carries the choice across pages.
    """
    store: dict[str, Any] = {}
    prefs = Preferences(store)

    prefs.remember("pair_b", "DAI")

    assert Preferences(store).get("pair_b") == "DAI"
    assert Preferences(store).index("pair_b", ["WBTC", "DAI", "WETH"], fallback=0) == 1


def test_an_unremembered_control_opens_on_its_default() -> None:
    prefs = Preferences({})

    assert prefs.index("pair_a", ["WBTC", "WETH"], fallback=1) == 1
    assert prefs.get("pair_a", "WBTC") == "WBTC"


def test_a_remembered_token_that_has_gone_falls_back_instead_of_misselecting() -> None:
    """A stale index would quietly select a different token, which is worse
    than forgetting: the screen would look right and be wrong."""
    prefs = Preferences({"pref:token_symbol": "GONE"})

    assert prefs.index("token_symbol", ["WBTC", "WETH"], fallback=1) == 1


def test_an_out_of_range_fallback_does_not_crash_the_widget() -> None:
    prefs = Preferences({})

    assert prefs.index("x", ["WBTC"], fallback=7) == 0
    assert prefs.index("x", [], fallback=0) == 0


def test_a_remembered_multiselect_drops_tokens_that_are_gone() -> None:
    prefs = Preferences({"pref:portfolio_symbols": ["WBTC", "GONE"]})

    assert prefs.multiselect("portfolio_symbols", ["WBTC", "WETH"], ["WETH"]) == ["WBTC"]
    # Nothing remembered survives: fall back, narrowed to what exists.
    assert Preferences({}).multiselect("p", ["WBTC"], ["WBTC", "GONE"]) == ["WBTC"]


def test_reset_forgets_selections_and_nothing_else() -> None:
    """A pending backfill confirmation is not a preference.

    Clearing it would turn a harmless "back to defaults" button into one
    that cancels an action the user was halfway through.
    """
    store: dict[str, Any] = {
        "pref:pair_a": "WETH",
        "pref:pair_b": "DAI",
        "backfill_pending": 90,
        "news_pending": ["WBTC"],
    }
    prefs = Preferences(store)

    assert prefs.names() == ["pair_a", "pair_b"]
    assert prefs.reset() == 2
    assert store == {"backfill_pending": 90, "news_pending": ["WBTC"]}
    assert prefs.reset() == 0, "resetting twice is harmless"


# --- the fill cache on the token screen -------------------------------------


def test_the_token_view_reads_cached_fills_rather_than_interpolating() -> None:
    """The whole point of the table: a widget change is one indexed select."""
    rows = price_rows("WETH", 60)
    del rows[30:32]
    db = FakeDatabase(
        get_price_history=rows,
        coverage_report=[coverage_row("WETH")],
        get_token_hours_fill=[
            {
                "symbol": "WETH",
                "token_id": "0xweth",
                "period_start_unix": BASE + i * HOUR,
                "method": "linear",
                "price_usd": Decimal("123"),
                "close": None,
                "gap_hours": 2,
                "computed_at": datetime(2026, 11, 7, tzinfo=UTC),
            }
            for i in (30, 31)
        ],
    )

    view = build_token_view(db, "WETH", None, window=5)

    assert view.filled_hours == 2
    assert view.fill_method == "linear"
    assert any(label == "get_token_hours_fill" for label, _ in db.fetches)


def test_an_empty_fill_cache_is_a_normal_state_not_an_error() -> None:
    rows = price_rows("WETH", 60)
    del rows[30:32]
    db = FakeDatabase(get_price_history=rows, coverage_report=[coverage_row("WETH")])

    view = build_token_view(db, "WETH", None, window=5)

    assert view.filled_hours == 0
    assert view.fill_method is None
    assert view.fill_overlay.empty
    assert view.missing_hours == 2, "the gap is still reported"


def test_the_overlay_anchors_each_filled_run_on_the_real_bars_beside_it() -> None:
    """A bare scatter inside a gap says nothing about what it was drawn
    between; a line that starts and ends on an observation says exactly that."""
    index = pd.date_range("2026-01-01", periods=6, freq="h", tz="UTC")
    prices = pd.Series([10.0, 11.0, 14.0, 15.0], index=index[[0, 1, 4, 5]], name="WBTC")
    fills = pd.DataFrame(
        {
            "symbol": ["WBTC", "WBTC"],
            "timestamp": [index[2], index[3]],
            "period_start_unix": [0, 0],
            "price_usd": [12.0, 13.0],
            "gap_hours": [2, 2],
            "method": ["linear", "linear"],
        }
    )

    overlay = fill_overlay(prices, fills)

    drawn = overlay.dropna()
    assert list(drawn.index) == list(index[1:5]), "the two fills plus one real bar each side"
    assert list(drawn) == [11.0, 12.0, 13.0, 14.0]
    assert pd.isna(overlay.loc[index[0]]), "bars away from the run are not drawn"


def test_the_overlay_is_empty_when_there_is_nothing_to_draw() -> None:
    prices = pd.Series([1.0, 2.0], index=pd.date_range("2026-01-01", periods=2, freq="h", tz="UTC"))

    assert fill_overlay(prices, pd.DataFrame()).empty
    assert fill_overlay(pd.Series(dtype="float64"), pd.DataFrame({"a": [1]})).empty


# --- the quality page's view of the cache -----------------------------------


def test_the_quality_view_knows_when_the_cache_is_behind_the_gaps(
    tokens_file: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    settings = make_settings(tokens_file=str(tokens_file), min_expected_coverage=0.95)

    view = build_quality_view(quality_db(), settings)

    assert view.cached_hours() == 0
    assert view.cached_hours("bridge") == 0
    assert view.cached_methods == [], "nothing computed yet"
    assert view.fillable_hours == 10, "one 10-hour gap, inside the 24h cap"
    # Per estimator, because the three are alternatives: a combined count
    # across all of them would read as 3x the gaps they actually cover.
    assert all(view.cache_is_stale(method) for method in ("linear", "causal", "bridge"))


def test_a_gap_longer_than_the_cap_is_not_counted_as_fillable(
    tokens_file: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Beyond the cap a straight line is fiction, so it is not work pending."""
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    db = FakeDatabase(
        coverage_report=[coverage_row("WBTC", stored=60, expected=100)],
        missing_hours=[{"period_start_unix": BASE + i * HOUR} for i in range(10, 50)],
    )

    view = build_quality_view(db, make_settings(tokens_file=str(tokens_file)))

    assert view.fillable_hours == 0, "a 40-hour run is past the cap"


def test_refreshing_the_cache_from_the_ui_refuses_an_empty_selection() -> None:
    with pytest.raises(ViewError, match="No tokens"):
        run_fill_refresh_now(FakeDatabase(), [])


# --- choosing between the three estimators ----------------------------------


def fill_row(index: int, method: str, price: str, sigma: str | None) -> dict[str, Any]:
    return {
        "symbol": "WETH",
        "token_id": "0xweth",
        "period_start_unix": BASE + index * HOUR,
        "method": method,
        "price_usd": Decimal(price),
        "close": None,
        "sigma_log": None if sigma is None else Decimal(sigma),
        "gap_hours": 2,
        "computed_at": datetime(2026, 11, 8, tzinfo=UTC),
    }


def token_db(method: str, sigma: str | None) -> FakeDatabase:
    rows = price_rows("WETH", 60)
    del rows[30:32]
    return FakeDatabase(
        get_price_history=rows,
        coverage_report=[coverage_row("WETH")],
        get_token_hours_fill=[fill_row(i, method, "123", sigma) for i in (30, 31)],
    )


def test_only_the_selected_estimator_is_read() -> None:
    """Exactly one at a time — they are alternatives, not layers."""
    db = token_db("causal", "0.01")

    view = build_token_view(db, "WETH", None, window=5, fill_method="causal")

    assert view.fill_method == "causal"
    assert view.filled_hours == 2
    asked = [params for label, params in db.fetches if label == "get_token_hours_fill"]
    assert asked and all(params["method"] == "causal" for params in asked)


def test_an_estimator_with_an_error_bar_produces_a_price_ribbon() -> None:
    """Stored in log space, so the band is multiplicative and never negative."""
    view = build_token_view(token_db("bridge", "0.01"), "WETH", None, window=5, fill_method="bridge")

    assert view.has_band
    ribbon = view.fill_band()
    assert list(ribbon.columns) == ["lower", "upper"]
    assert len(ribbon) == 2
    assert (ribbon["lower"] > 0).all(), "a price band cannot cross zero"
    assert (ribbon["lower"] < 123.0).all() and (ribbon["upper"] > 123.0).all()
    # exp(+/-0.01) around 123.
    assert ribbon["upper"].iloc[0] == pytest.approx(123.0 * np.exp(0.01))


def test_an_estimator_without_an_error_bar_draws_no_ribbon() -> None:
    """A straight line cannot say how unsure it is, and must not pretend to."""
    view = build_token_view(token_db("linear", None), "WETH", None, window=5, fill_method="linear")

    assert view.filled_hours == 2, "the estimate is still drawn"
    assert not view.has_band
    assert view.fill_band().empty


def test_a_wider_band_is_asymmetric_in_price_terms() -> None:
    """Because the model is of log prices: a 50% fall and a 100% rise are
    the same move, and the ribbon has to show that."""
    view = build_token_view(token_db("causal", "0.5"), "WETH", None, window=5, fill_method="causal")

    ribbon = view.fill_band()
    above = ribbon["upper"].iloc[0] - 123.0
    below = 123.0 - ribbon["lower"].iloc[0]
    assert above > below


# --- the catalogue and the measured comparison ------------------------------


def test_the_catalogue_names_every_estimator_and_who_may_consume_it() -> None:
    catalogue = method_catalogue().set_index("method")

    assert list(catalogue.index) == ["linear", "causal", "bridge"]
    assert catalogue.loc["causal", "consumable_by"] == "a strategy"
    assert catalogue.loc["linear", "consumable_by"] == "display only"
    assert catalogue.loc["bridge", "consumable_by"] == "display only"
    assert catalogue.loc["linear", "error_bar"] == "no"
    assert catalogue.loc["bridge", "error_bar"] == "yes"


def test_the_measured_comparison_ranks_the_estimators_best_first() -> None:
    db = FakeDatabase(
        get_price_history=price_rows("WETH", 900) + price_rows("WBTC", 900, seed=9),
        coverage_report=[coverage_row("WETH"), coverage_row("WBTC")],
    )

    scores = build_method_scores(db, ["WETH", "WBTC"])

    assert set(scores["method"]) == {"linear", "causal", "bridge"}
    assert scores["mae_bps"].is_monotonic_increasing, "best first"
    # Pooled across both tokens rather than taken from whichever ran last.
    assert (scores["hours"] > 0).all()
    assert scores.loc[scores["method"] == "causal", "consumable_by"].iloc[0] == "a strategy"


def test_the_comparison_is_empty_rather_than_failing_on_a_fresh_database() -> None:
    assert build_method_scores(FakeDatabase(), ["WETH"]).empty
    assert build_method_scores(FakeDatabase(), []).empty


def test_refreshing_rejects_an_estimator_that_does_not_exist() -> None:
    with pytest.raises(ViewError, match="unknown estimator"):
        run_fill_refresh_now(FakeDatabase(), ["WETH"], method="telepathy")
