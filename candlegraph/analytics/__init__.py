"""Deterministic analytics over stored price history.

Everything here is a pure function of its inputs: same series in, same numbers
out, no clock and no I/O. That is what makes the results reproducible from the
CLI and safe to call from the UI.

The one module that touches the database is :mod:`candlegraph.analytics.prices`,
and it only reads.
"""

from candlegraph.analytics.arima import ArimaConfig, ArimaForecast, fit_forecast, select_order
from candlegraph.analytics.portfolio import (
    FrontierPoint,
    PortfolioConfig,
    PortfolioResult,
    efficient_frontier,
    optimise_portfolio,
)
from candlegraph.analytics.prices import PriceHistory, load_price_frame
from candlegraph.analytics.rolling import (
    HOURS_PER_YEAR,
    bollinger_bands,
    drawdown_curve,
    log_returns,
    max_drawdown,
    rolling_mean,
    rolling_volatility,
    rolling_zscore,
    sharpe_ratio,
    simple_returns,
)

__all__ = [
    "HOURS_PER_YEAR",
    "ArimaConfig",
    "ArimaForecast",
    "FrontierPoint",
    "PortfolioConfig",
    "PortfolioResult",
    "PriceHistory",
    "bollinger_bands",
    "drawdown_curve",
    "efficient_frontier",
    "fit_forecast",
    "load_price_frame",
    "log_returns",
    "max_drawdown",
    "optimise_portfolio",
    "rolling_mean",
    "rolling_volatility",
    "rolling_zscore",
    "select_order",
    "sharpe_ratio",
    "simple_returns",
]
