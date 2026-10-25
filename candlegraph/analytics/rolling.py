"""Rolling statistics and risk measures over a price series.

Two conventions hold throughout and are worth stating once:

* **Windows are complete or absent.** Every rolling function uses
  ``min_periods=window``, so the first ``window-1`` points are ``NaN`` rather
  than a mean of two observations pretending to be a mean of twenty. A partial
  window is not a cheap approximation, it is a different statistic.
* **Sample, not population.** Standard deviations use ``ddof=1``. With a
  20-period window the difference is ~2.6%, which is enough to move a
  Bollinger band across a price.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Hourly candles: the annualisation factor for every rate below.
HOURS_PER_YEAR = 24 * 365


def periods_per_year(interval_hours: int = 1) -> float:
    """Annualisation factor for candles ``interval_hours`` wide."""
    if interval_hours <= 0:
        raise ValueError("interval_hours must be positive")
    return HOURS_PER_YEAR / interval_hours


def _require_window(window: int) -> None:
    if window < 2:
        raise ValueError("window must be at least 2")


def simple_returns(prices: pd.Series) -> pd.Series:
    """Period-over-period arithmetic returns."""
    return prices.astype("float64").pct_change()


def log_returns(prices: pd.Series) -> pd.Series:
    """Period-over-period log returns.

    Preferred for volatility work: they are additive across time, so scaling
    by ``sqrt(periods)`` is exact rather than an approximation.
    """
    values = prices.astype("float64")
    # A non-positive price is not a price. Leave it as NaN rather than
    # emitting -inf and poisoning every window that touches it.
    safe = values.where(values > 0)
    logged = pd.Series(np.log(safe.to_numpy()), index=safe.index, name=safe.name)
    return logged.diff()


def rolling_mean(series: pd.Series, window: int) -> pd.Series:
    """Rolling arithmetic mean over complete windows only."""
    _require_window(window)
    return series.astype("float64").rolling(window=window, min_periods=window).mean()


def rolling_std(series: pd.Series, window: int) -> pd.Series:
    """Rolling sample standard deviation (``ddof=1``) over complete windows."""
    _require_window(window)
    return series.astype("float64").rolling(window=window, min_periods=window).std(ddof=1)


def rolling_volatility(
    returns: pd.Series, window: int, interval_hours: int = 1, annualise: bool = True
) -> pd.Series:
    """Rolling volatility of a return series.

    Args:
        returns: Period returns, usually from :func:`log_returns`.
        window: Number of periods per window.
        interval_hours: Width of one period, for the annualisation factor.
        annualise: Scale by ``sqrt(periods per year)``. Turn off to get the
            raw per-period standard deviation.

    Returns:
        Volatility aligned to ``returns``, ``NaN`` until the first full window.
    """
    volatility = rolling_std(returns, window)
    if not annualise:
        return volatility
    scaled: pd.Series = volatility * float(np.sqrt(periods_per_year(interval_hours)))
    return scaled


def rolling_zscore(series: pd.Series, window: int) -> pd.Series:
    """How many rolling standard deviations each point sits from its mean.

    A flat window has zero dispersion; the z-score there is undefined rather
    than infinite, so those points come back as ``NaN``.
    """
    mean = rolling_mean(series, window)
    std = rolling_std(series, window)
    zscore: pd.Series = (series.astype("float64") - mean) / std.where(std > 0)
    return zscore


def bollinger_bands(prices: pd.Series, window: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    """Bollinger bands plus the z-score and bandwidth that drive the strategy.

    Args:
        prices: Close prices.
        window: Lookback for the middle band.
        num_std: Band half-width in sample standard deviations.

    Returns:
        A frame indexed like ``prices`` with ``middle``, ``upper``, ``lower``,
        ``zscore``, ``bandwidth`` (band width as a fraction of the middle), and
        ``percent_b`` (0 at the lower band, 1 at the upper).
    """
    _require_window(window)
    if num_std <= 0:
        raise ValueError("num_std must be positive")

    values = prices.astype("float64")
    middle = rolling_mean(values, window)
    std = rolling_std(values, window)
    upper = middle + num_std * std
    lower = middle - num_std * std
    width = (upper - lower).where(lambda w: w > 0)

    return pd.DataFrame(
        {
            "middle": middle,
            "upper": upper,
            "lower": lower,
            "zscore": (values - middle) / std.where(std > 0),
            "bandwidth": (upper - lower) / middle.where(middle != 0),
            "percent_b": (values - lower) / width,
        }
    )


def rolling_summary(prices: pd.Series, window: int = 24, interval_hours: int = 1) -> pd.DataFrame:
    """Rolling mean price plus rolling annualised volatility, side by side.

    This is the "rolling volatility and mean" view the UI and CLI render.
    """
    returns = log_returns(prices)
    return pd.DataFrame(
        {
            "price": prices.astype("float64"),
            "rolling_mean": rolling_mean(prices, window),
            "rolling_return": rolling_mean(returns, window),
            "rolling_volatility": rolling_volatility(returns, window, interval_hours),
        }
    )


def sharpe_ratio(returns: pd.Series, risk_free_rate: float = 0.0, interval_hours: int = 1) -> float:
    """Annualised Sharpe ratio of a per-period return series.

    Args:
        returns: Per-period returns.
        risk_free_rate: Annual risk-free rate, e.g. ``0.04`` for 4%. It is
            converted to the series' own period before subtraction, which is
            the only way the subtraction is dimensionally meaningful.
        interval_hours: Width of one period.

    Returns:
        The annualised ratio, or ``0.0`` when there is no dispersion to divide
        by (fewer than two observations, or a perfectly flat series).
    """
    clean = returns.dropna().astype("float64")
    if len(clean) < 2:
        return 0.0

    factor = periods_per_year(interval_hours)
    excess = clean - risk_free_rate / factor
    deviation = float(excess.std(ddof=1))
    if deviation == 0 or not np.isfinite(deviation):
        return 0.0
    return float(excess.mean() / deviation * np.sqrt(factor))


def sortino_ratio(returns: pd.Series, risk_free_rate: float = 0.0, interval_hours: int = 1) -> float:
    """Like :func:`sharpe_ratio` but penalising only downside deviation."""
    clean = returns.dropna().astype("float64")
    if len(clean) < 2:
        return 0.0

    factor = periods_per_year(interval_hours)
    excess = clean - risk_free_rate / factor
    downside = excess[excess < 0]
    if len(downside) < 2:
        return 0.0
    deviation = float(np.sqrt((downside**2).mean()))
    if deviation == 0 or not np.isfinite(deviation):
        return 0.0
    return float(excess.mean() / deviation * np.sqrt(factor))


def equity_curve(returns: pd.Series, initial: float = 1.0) -> pd.Series:
    """Compound a return series into a wealth index."""
    return initial * (1.0 + returns.fillna(0.0).astype("float64")).cumprod()


def drawdown_curve(equity: pd.Series) -> pd.Series:
    """Fractional drawdown from the running peak, as a non-positive series."""
    values = equity.astype("float64")
    peak = values.cummax()
    return (values / peak.where(peak != 0) - 1.0).fillna(0.0)


def max_drawdown(equity: pd.Series) -> float:
    """The worst peak-to-trough fall, as a non-positive fraction."""
    if equity.empty:
        return 0.0
    return float(drawdown_curve(equity).min())
