"""The UI's view-models.

All the UI's arithmetic lives here rather than in the Streamlit script, so
this is where the UI gets its coverage. Errors a user can fix must arrive as
:class:`ViewError` with a readable message, not as a traceback on screen.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
import pytest

from candlegraph.analytics.arima import ArimaConfig, ArimaForecast
from candlegraph.analytics.portfolio import PortfolioConfig
from candlegraph.analytics.risk import RiskConfig
from candlegraph.strategies.backtest import BacktestConfig
from candlegraph.strategies.bollinger import BollingerConfig
from candlegraph.ui.viewmodels import (
    ViewError,
    available_symbols,
    build_agents_view,
    build_forecast,
    build_market_view,
    build_news_view,
    build_pair_view,
    build_portfolio_view,
    build_risk_view,
    build_strategy_view,
    build_token_view,
    candle_frame,
    default_index,
    default_max_weight,
    default_portfolio_symbols,
    forecast_frame,
    load_prices,
    run_backfill,
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
