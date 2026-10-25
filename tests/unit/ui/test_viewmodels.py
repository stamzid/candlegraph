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
from candlegraph.strategies.backtest import BacktestConfig
from candlegraph.strategies.bollinger import BollingerConfig
from candlegraph.ui.viewmodels import (
    ViewError,
    available_symbols,
    build_forecast,
    build_portfolio_view,
    build_price_view,
    build_strategy_view,
    forecast_frame,
    load_prices,
)
from tests.conftest import FakeDatabase

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
            "high": Decimal(str(p)),
            "low": Decimal(str(p)),
            "close": Decimal(str(p)),
            "price_usd": Decimal(str(p)),
            "volume_usd": Decimal("1000"),
            "total_value_locked_usd": Decimal("5000"),
        }
        for i, p in enumerate(walk)
    ]


@pytest.fixture
def db() -> FakeDatabase:
    return FakeDatabase(
        get_price_history=price_rows("WETH", 400) + price_rows("WBTC", 400, seed=4),
        coverage_report=[
            {
                "token_id": "0xw",
                "symbol": "WETH",
                "stored_hours": 400,
                "first_unix": BASE,
                "last_unix": BASE + 399 * HOUR,
                "expected_hours": 400,
            },
            {
                "token_id": "0xb",
                "symbol": "WBTC",
                "stored_hours": 400,
                "first_unix": BASE,
                "last_unix": BASE + 399 * HOUR,
                "expected_hours": 400,
            },
        ],
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


# --- the price view --------------------------------------------------------


def test_the_price_view_carries_prices_bands_and_summary(db: FakeDatabase) -> None:
    view = build_price_view(db, "WETH", days=30, window=24)

    assert view.symbol == "WETH"
    assert view.bars == 400
    assert {"price", "rolling_mean", "rolling_volatility"} <= set(view.summary.columns)
    assert {"middle", "upper", "lower", "zscore"} <= set(view.bands.columns)


def test_the_latest_row_skips_incomplete_windows(db: FakeDatabase) -> None:
    latest = build_price_view(db, "WETH", days=30, window=24).latest

    assert np.isfinite(latest["rolling_mean"])
    assert np.isfinite(latest["rolling_volatility"])


def test_a_window_longer_than_the_history_says_so(db: FakeDatabase) -> None:
    with pytest.raises(ViewError, match="fewer than the 500-bar window"):
        build_price_view(db, "WETH", days=30, window=500)


def test_an_unknown_token_is_a_readable_error(db: FakeDatabase) -> None:
    with pytest.raises(ViewError, match="No stored price history for DOGE"):
        build_price_view(db, "DOGE", days=30, window=24)


def test_band_width_is_configurable(db: FakeDatabase) -> None:
    narrow = build_price_view(db, "WETH", 30, 24, num_std=1.0).bands.dropna().iloc[-1]
    wide = build_price_view(db, "WETH", 30, 24, num_std=3.0).bands.dropna().iloc[-1]

    assert wide["upper"] > narrow["upper"]


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
