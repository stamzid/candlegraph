"""Everything the UI computes, with no Streamlit in sight.

Streamlit scripts are hard to test: they are top-to-bottom modules whose
output is a side effect on a global session. So all the work lives here as
plain functions returning plain data, and ``app.py`` stays a thin layer of
widgets and charts over them. These functions carry the test coverage; the
rendering does not need any.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from candlegraph.analytics.arima import ArimaConfig, ArimaForecast, fit_forecast
from candlegraph.analytics.portfolio import (
    FrontierPoint,
    PortfolioConfig,
    PortfolioError,
    PortfolioResult,
    efficient_frontier,
    optimise_portfolio,
)
from candlegraph.analytics.prices import load_price_frame
from candlegraph.analytics.rolling import bollinger_bands, log_returns, rolling_summary
from candlegraph.storage import DatabaseManager
from candlegraph.strategies import backtest, run_strategy
from candlegraph.strategies.backtest import BacktestConfig, BacktestResult
from candlegraph.strategies.base import StrategyResult


class ViewError(RuntimeError):
    """Something the user needs to fix, phrased for the screen rather than a log."""


@dataclass
class PriceView:
    """A token's price history plus its rolling statistics."""

    symbol: str
    prices: pd.Series
    summary: pd.DataFrame
    bands: pd.DataFrame
    window: int

    @property
    def bars(self) -> int:
        return len(self.prices)

    @property
    def latest(self) -> dict[str, float]:
        """Headline numbers for the metric row, skipping incomplete windows."""
        complete = self.summary.dropna()
        if complete.empty:
            return {"price": float(self.prices.iloc[-1]) if self.bars else 0.0}
        row = complete.iloc[-1]
        return {
            "price": float(row["price"]),
            "rolling_mean": float(row["rolling_mean"]),
            "rolling_volatility": float(row["rolling_volatility"]),
            "rolling_return": float(row["rolling_return"]),
        }


@dataclass
class PortfolioView:
    """A solved allocation, optionally with a frontier to plot beside it."""

    result: PortfolioResult
    frontier: list[FrontierPoint] = field(default_factory=list)
    bars: int = 0
    symbols: tuple[str, ...] = ()

    @property
    def weights_frame(self) -> pd.DataFrame:
        """Non-zero weights, largest first, ready for a bar chart."""
        held = {k: v for k, v in self.result.weights.items() if abs(v) > 1e-6}
        frame = pd.DataFrame({"weight": pd.Series(held, dtype="float64")})
        return frame.sort_values("weight", ascending=False)

    @property
    def frontier_frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {"volatility": p.volatility, "expected_return": p.expected_return, "sharpe": p.sharpe}
                for p in self.frontier
            ]
        )


@dataclass
class StrategyView:
    """A strategy run and its backtest, kept together for one screen."""

    strategy: StrategyResult
    report: BacktestResult

    @property
    def equity_frame(self) -> pd.DataFrame:
        """Strategy equity against buy-and-hold, both rebased to the same start."""
        frame = self.report.frame
        capital = self.report.config.initial_capital
        hold = capital * (1.0 + frame["returns"].fillna(0.0)).cumprod()
        return pd.DataFrame({"strategy": frame["equity"], "buy and hold": hold})


def available_symbols(db: DatabaseManager) -> list[str]:
    """Symbols that actually have stored rows, newest-coverage first.

    Reads what is in the database rather than what is configured: a token
    added to the config an hour ago has nothing to plot yet.
    """
    rows = db.fetch_all(db.sql.coverage_report, {"start_unix": None})
    return sorted(row["symbol"] for row in rows if row.get("symbol"))


def load_prices(db: DatabaseManager, symbols: list[str], days: int | None) -> pd.DataFrame:
    """Wide price frame, raising a user-facing error when there is nothing."""
    if not symbols:
        raise ViewError("Pick at least one token.")
    frame = load_price_frame(db, symbols, days=days)
    if frame.empty:
        raise ViewError(f"No stored price history for {', '.join(symbols)} in that window.")
    return frame


def build_price_view(
    db: DatabaseManager,
    symbol: str,
    days: int | None,
    window: int,
    num_std: float = 2.0,
    interval_hours: int = 1,
) -> PriceView:
    """Price history plus rolling mean, volatility, and Bollinger bands."""
    frame = load_prices(db, [symbol], days)
    if symbol not in frame.columns:
        raise ViewError(f"No stored price history for {symbol} in that window.")

    prices = frame[symbol].dropna()
    prices.name = symbol
    if len(prices) < window:
        raise ViewError(
            f"{symbol} has {len(prices)} bars, fewer than the {window}-bar window. "
            "Widen the date range or shorten the window."
        )

    return PriceView(
        symbol=symbol,
        prices=prices,
        summary=rolling_summary(prices, window=window, interval_hours=interval_hours),
        bands=bollinger_bands(prices, window=window, num_std=num_std),
        window=window,
    )


def build_portfolio_view(
    db: DatabaseManager,
    symbols: list[str],
    days: int | None,
    config: PortfolioConfig,
    frontier_points: int = 0,
) -> PortfolioView:
    """Optimise across the chosen tokens, optionally tracing the frontier."""
    if len(symbols) < 2:
        raise ViewError("A portfolio needs at least two tokens.")

    prices = load_prices(db, symbols, days)
    returns = prices.apply(log_returns).dropna(how="all")

    try:
        result = optimise_portfolio(returns, config)
        frontier = efficient_frontier(returns, config, points=frontier_points) if frontier_points >= 2 else []
    except PortfolioError as exc:
        raise ViewError(str(exc)) from exc

    return PortfolioView(
        result=result,
        frontier=frontier,
        bars=len(returns.dropna(how="any")),
        symbols=tuple(prices.columns),
    )


def build_strategy_view(
    db: DatabaseManager,
    symbol: str,
    strategy: str,
    days: int | None,
    strategy_config: Any = None,
    backtest_config: BacktestConfig | None = None,
) -> StrategyView:
    """Run a strategy over stored prices and backtest it in one step."""
    frame = load_prices(db, [symbol], days)
    if symbol not in frame.columns:
        raise ViewError(f"No stored price history for {symbol} in that window.")

    prices = frame[symbol].dropna()
    prices.name = symbol

    result = run_strategy(strategy, prices, strategy_config)
    return StrategyView(strategy=result, report=backtest(result, config=backtest_config))


def build_forecast(
    db: DatabaseManager, symbol: str, days: int | None, config: ArimaConfig
) -> tuple[pd.Series, ArimaForecast]:
    """Price history and an ARIMA forecast over it."""
    frame = load_prices(db, [symbol], days)
    if symbol not in frame.columns:
        raise ViewError(f"No stored price history for {symbol} in that window.")

    prices = frame[symbol].dropna()
    prices.name = symbol
    return prices, fit_forecast(prices, config)


def forecast_frame(prices: pd.Series, forecast: ArimaForecast) -> pd.DataFrame:
    """The forecast path on a future hourly index, for plotting past the data."""
    if not forecast.ok or not forecast.path:
        return pd.DataFrame(columns=["forecast"])

    last = pd.DatetimeIndex(prices.index)[-1]
    index = pd.date_range(last + pd.Timedelta(hours=1), periods=len(forecast.path), freq="h")
    return pd.DataFrame({"forecast": list(forecast.path)}, index=index)
