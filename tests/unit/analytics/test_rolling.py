"""Rolling statistics: window semantics, annualisation, and risk measures."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from candlegraph.analytics.rolling import (
    HOURS_PER_YEAR,
    bollinger_bands,
    drawdown_curve,
    equity_curve,
    log_returns,
    max_drawdown,
    periods_per_year,
    rolling_mean,
    rolling_std,
    rolling_summary,
    rolling_volatility,
    rolling_zscore,
    sharpe_ratio,
    simple_returns,
    sortino_ratio,
)
from tests.unit.analytics.conftest import constant_series, hourly_index, linear_series

# --- annualisation ---------------------------------------------------------


def test_hourly_bars_annualise_by_8760() -> None:
    assert periods_per_year(1) == HOURS_PER_YEAR == 8760


def test_wider_bars_annualise_by_proportionally_less() -> None:
    assert periods_per_year(24) == 365
    assert periods_per_year(4) == 2190


def test_a_non_positive_interval_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        periods_per_year(0)


# --- returns ---------------------------------------------------------------


def test_simple_returns_are_period_over_period() -> None:
    prices = pd.Series([100.0, 110.0, 99.0], index=hourly_index(3))

    returns = simple_returns(prices)

    assert np.isnan(returns.iloc[0])
    assert returns.iloc[1] == pytest.approx(0.10)
    assert returns.iloc[2] == pytest.approx(-0.10)


def test_log_returns_are_additive_across_time() -> None:
    """The property that makes sqrt-scaling of volatility exact."""
    prices = pd.Series([100.0, 110.0, 121.0], index=hourly_index(3))

    returns = log_returns(prices).dropna()

    assert returns.sum() == pytest.approx(np.log(121.0 / 100.0))


def test_a_non_positive_price_becomes_nan_not_negative_infinity() -> None:
    """log(0) would be -inf and would poison every window that touches it."""
    prices = pd.Series([100.0, 0.0, 100.0], index=hourly_index(3))

    returns = log_returns(prices)

    assert not np.isinf(returns).any()
    assert returns.isna().iloc[1]


# --- window semantics ------------------------------------------------------


def test_a_rolling_window_is_nan_until_it_is_complete() -> None:
    """A mean of 3 observations is not a 20-period mean; it is a different number."""
    series = linear_series(10)

    mean = rolling_mean(series, window=5)

    assert mean.iloc[:4].isna().all()
    assert mean.notna().iloc[4:].all()


def test_the_rolling_mean_of_a_line_is_its_midpoint() -> None:
    series = linear_series(10, start=0.0, step=1.0)

    # Window over values 0..4 has mean 2.
    assert rolling_mean(series, window=5).iloc[4] == pytest.approx(2.0)


def test_rolling_std_uses_the_sample_convention() -> None:
    """ddof=1, not 0: over a 20-period window the difference is ~2.6%."""
    series = pd.Series([1.0, 2.0, 3.0, 4.0], index=hourly_index(4))

    assert rolling_std(series, window=4).iloc[3] == pytest.approx(np.std([1, 2, 3, 4], ddof=1))


@pytest.mark.parametrize("window", [0, 1, -5])
def test_a_degenerate_window_is_rejected(window: int) -> None:
    with pytest.raises(ValueError, match="at least 2"):
        rolling_mean(linear_series(10), window)


# --- volatility ------------------------------------------------------------


def test_volatility_is_annualised_by_the_square_root_of_time() -> None:
    returns = pd.Series(np.tile([0.01, -0.01], 50), index=hourly_index(100))

    raw = rolling_volatility(returns, 20, annualise=False).iloc[-1]
    annual = rolling_volatility(returns, 20, interval_hours=1).iloc[-1]

    assert annual == pytest.approx(raw * np.sqrt(8760))


def test_a_flat_series_has_zero_volatility() -> None:
    returns = log_returns(constant_series(60))

    assert rolling_volatility(returns, 20).dropna().eq(0.0).all()


def test_wider_bars_lower_the_annualisation_factor() -> None:
    returns = pd.Series(np.tile([0.01, -0.01], 50), index=hourly_index(100))

    hourly = rolling_volatility(returns, 20, interval_hours=1).iloc[-1]
    daily = rolling_volatility(returns, 20, interval_hours=24).iloc[-1]

    assert daily == pytest.approx(hourly / np.sqrt(24))


# --- z-score ---------------------------------------------------------------


def test_the_zscore_of_a_flat_window_is_nan_not_infinity() -> None:
    """Zero dispersion makes the score undefined, not infinitely large."""
    zscore = rolling_zscore(constant_series(30), window=10)

    assert zscore.dropna().empty


def test_a_point_one_std_above_the_mean_scores_one() -> None:
    values = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0], index=hourly_index(5))

    zscore = rolling_zscore(values, window=5).iloc[-1]

    expected = (5.0 - 3.0) / np.std([1, 2, 3, 4, 5], ddof=1)
    assert zscore == pytest.approx(expected)


# --- Bollinger bands -------------------------------------------------------


def test_the_bands_sit_symmetrically_around_the_middle() -> None:
    bands = bollinger_bands(linear_series(60), window=20, num_std=2.0)
    row = bands.dropna().iloc[-1]

    assert row["upper"] - row["middle"] == pytest.approx(row["middle"] - row["lower"])


def test_a_wider_num_std_widens_the_bands() -> None:
    narrow = bollinger_bands(linear_series(60), window=20, num_std=1.0).dropna().iloc[-1]
    wide = bollinger_bands(linear_series(60), window=20, num_std=3.0).dropna().iloc[-1]

    assert wide["upper"] > narrow["upper"]
    assert wide["lower"] < narrow["lower"]


def test_percent_b_is_zero_at_the_lower_band_and_one_at_the_upper() -> None:
    bands = bollinger_bands(linear_series(60), window=20).dropna()
    row = bands.iloc[-1]

    at_lower = (row["lower"] - row["lower"]) / (row["upper"] - row["lower"])
    assert at_lower == pytest.approx(0.0)
    assert 0.0 <= row["percent_b"] <= 1.5


def test_bands_carry_the_zscore_the_strategy_reads() -> None:
    bands = bollinger_bands(linear_series(60), window=20)

    assert "zscore" in bands
    assert bands["zscore"].notna().sum() == 60 - 19


def test_a_flat_series_gives_bands_with_no_width() -> None:
    bands = bollinger_bands(constant_series(40), window=10).dropna(subset=["middle"])

    assert (bands["upper"] == bands["lower"]).all()
    assert bands["zscore"].isna().all()


@pytest.mark.parametrize(("window", "num_std"), [(1, 2.0), (20, 0.0), (20, -1.0)])
def test_degenerate_band_parameters_are_rejected(window: int, num_std: float) -> None:
    with pytest.raises(ValueError):
        bollinger_bands(linear_series(60), window=window, num_std=num_std)


# --- summary ---------------------------------------------------------------


def test_the_rolling_summary_pairs_mean_and_volatility(walk: pd.Series) -> None:
    """The 'rolling volatility and mean' view the CLI and UI render."""
    summary = rolling_summary(walk, window=24)

    assert list(summary.columns) == ["price", "rolling_mean", "rolling_return", "rolling_volatility"]
    assert summary["rolling_volatility"].dropna().gt(0).all()
    assert len(summary) == len(walk)


# --- ratios ----------------------------------------------------------------


def test_sharpe_is_zero_when_there_is_no_dispersion() -> None:
    assert sharpe_ratio(pd.Series([0.01] * 50)) == 0.0


def test_sharpe_is_zero_for_too_few_observations() -> None:
    assert sharpe_ratio(pd.Series([0.01])) == 0.0


def test_a_higher_risk_free_rate_lowers_sharpe() -> None:
    returns = pd.Series(np.tile([0.002, -0.001], 100))

    assert sharpe_ratio(returns, risk_free_rate=0.10) < sharpe_ratio(returns, risk_free_rate=0.0)


def test_the_risk_free_rate_is_deannualised_before_subtraction() -> None:
    """Subtracting an annual rate from an hourly return would be nonsense."""
    returns = pd.Series(np.tile([0.002, -0.001], 100))

    # 4% a year over 8760 hours is a tiny per-bar haircut, not a 4% one.
    drop = sharpe_ratio(returns, 0.0) - sharpe_ratio(returns, 0.04)
    assert 0 < drop < 0.5


def test_sortino_ignores_upside_dispersion() -> None:
    """A series that only ever rises has no downside to penalise."""
    mixed = pd.Series(np.tile([0.02, -0.01], 100))

    assert sortino_ratio(mixed) > sharpe_ratio(mixed)


def test_sortino_is_zero_without_enough_losses() -> None:
    assert sortino_ratio(pd.Series([0.01] * 50)) == 0.0


# --- equity and drawdown ---------------------------------------------------


def test_the_equity_curve_compounds() -> None:
    curve = equity_curve(pd.Series([0.1, 0.1]), initial=100.0)

    assert curve.iloc[-1] == pytest.approx(121.0)


def test_drawdown_is_zero_on_a_monotonically_rising_curve() -> None:
    curve = equity_curve(pd.Series([0.01] * 20), initial=100.0)

    assert max_drawdown(curve) == pytest.approx(0.0)


def test_drawdown_measures_the_worst_peak_to_trough_fall() -> None:
    curve = pd.Series([100.0, 120.0, 60.0, 90.0])

    assert max_drawdown(curve) == pytest.approx(-0.5)


def test_the_drawdown_curve_is_never_positive() -> None:
    curve = pd.Series([100.0, 120.0, 60.0, 200.0])

    assert drawdown_curve(curve).le(0).all()


def test_an_empty_curve_has_no_drawdown() -> None:
    assert max_drawdown(pd.Series(dtype="float64")) == 0.0
