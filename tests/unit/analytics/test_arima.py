"""ARIMA fitting: configuration, order selection, and predictable failure."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from candlegraph.analytics.arima import ArimaConfig, ArimaError, ArimaForecast, fit_forecast, select_order
from tests.unit.analytics.conftest import constant_series, hourly_index, linear_series, random_walk

# --- configuration ---------------------------------------------------------


@pytest.mark.parametrize("order", [(1, 1), (-1, 1, 1), (1, 1, 1, 1)])
def test_a_malformed_order_is_rejected(order: tuple) -> None:
    with pytest.raises(ArimaError, match="order must be"):
        ArimaConfig(order=order)  # type: ignore[arg-type]


def test_an_unknown_criterion_is_rejected() -> None:
    with pytest.raises(ArimaError, match="unknown criterion"):
        ArimaConfig(criterion="rmse")  # type: ignore[arg-type]


@pytest.mark.parametrize("confidence", [0.0, 1.0, 1.5, -0.1])
def test_a_confidence_outside_zero_to_one_is_rejected(confidence: float) -> None:
    with pytest.raises(ArimaError, match="confidence"):
        ArimaConfig(confidence=confidence)


def test_a_zero_horizon_is_rejected() -> None:
    with pytest.raises(ArimaError, match="forecast_steps"):
        ArimaConfig(forecast_steps=0)


def test_an_absurdly_small_history_requirement_is_rejected() -> None:
    with pytest.raises(ArimaError, match="min_observations"):
        ArimaConfig(min_observations=5)


def test_the_defaults_are_a_log_price_random_walk_with_drift_terms() -> None:
    config = ArimaConfig()

    assert config.order == (1, 1, 1)
    assert config.use_log_prices is True
    assert config.trend == "n"
    assert config.forecast_steps == 1


# --- failure modes ---------------------------------------------------------


def test_too_little_history_returns_not_ok_rather_than_raising() -> None:
    """The walk-forward loop must be able to skip a window and carry on."""
    result = fit_forecast(random_walk(20), ArimaConfig(min_observations=60))

    assert result.ok is False
    assert "need 60 observations" in result.message
    assert np.isnan(result.predicted)


def test_a_not_ok_forecast_implies_no_trade() -> None:
    result = ArimaForecast(ok=False, order=(1, 1, 1))

    assert result.expected_return == 0.0
    assert result.interval_width == float("inf")


def test_a_degenerate_series_fails_softly() -> None:
    """A constant series has no variance for the likelihood to work with."""
    result = fit_forecast(constant_series(200), ArimaConfig(min_observations=60))

    assert result.ok is False or np.isfinite(result.predicted)


# --- fitting ---------------------------------------------------------------


def test_a_fit_returns_a_price_space_forecast() -> None:
    """Log fits must be exponentiated back, or predicted is not comparable to price."""
    prices = random_walk(300, start=100.0)

    result = fit_forecast(prices, ArimaConfig(use_log_prices=True))

    assert result.ok
    assert result.last_price == pytest.approx(float(prices.iloc[-1]))
    # A one-hour-ahead forecast lives in the neighbourhood of the last price.
    assert 0.5 * result.last_price < result.predicted < 2.0 * result.last_price


def test_the_confidence_interval_brackets_the_point_forecast() -> None:
    result = fit_forecast(random_walk(300), ArimaConfig())

    assert result.ok
    assert result.lower <= result.predicted <= result.upper


def test_a_wider_confidence_gives_a_wider_interval() -> None:
    prices = random_walk(300)

    narrow = fit_forecast(prices, ArimaConfig(confidence=0.80))
    wide = fit_forecast(prices, ArimaConfig(confidence=0.99))

    assert wide.interval_width > narrow.interval_width


def test_expected_return_is_relative_to_the_last_price() -> None:
    result = fit_forecast(random_walk(300), ArimaConfig())

    assert result.expected_return == pytest.approx(result.predicted / result.last_price - 1.0)


def test_a_multi_step_forecast_returns_the_whole_path() -> None:
    result = fit_forecast(random_walk(300), ArimaConfig(forecast_steps=5))

    assert result.ok
    assert len(result.path) == 5
    # `predicted` is the end of the horizon, not the first step.
    assert result.predicted == pytest.approx(result.path[-1])


def test_a_trending_line_forecasts_upwards() -> None:
    """The one case with an unambiguous right answer."""
    result = fit_forecast(linear_series(300, start=100.0, step=0.5), ArimaConfig(order=(1, 1, 0)))

    assert result.ok
    assert result.predicted > result.last_price


def test_fitting_without_logs_also_works() -> None:
    result = fit_forecast(random_walk(300), ArimaConfig(use_log_prices=False))

    assert result.ok
    assert np.isfinite(result.predicted)


def test_the_fit_does_not_mutate_the_input() -> None:
    prices = random_walk(300)
    before = prices.copy()

    fit_forecast(prices, ArimaConfig())

    pd.testing.assert_series_equal(prices, before)


def test_the_reported_criterion_value_is_finite() -> None:
    result = fit_forecast(random_walk(300), ArimaConfig(criterion="bic"))

    assert result.ok
    assert np.isfinite(result.criterion_value)


# --- order selection -------------------------------------------------------


def test_auto_order_stays_inside_the_configured_grid() -> None:
    config = ArimaConfig(auto_order=True, max_p=2, max_q=2, d=1)

    p, d, q = select_order(random_walk(300), config)

    assert 0 <= p <= 2
    assert 0 <= q <= 2
    assert d == 1


def test_auto_order_is_used_by_the_fit() -> None:
    config = ArimaConfig(auto_order=True, max_p=2, max_q=1, order=(3, 1, 3))

    result = fit_forecast(random_walk(300), config)

    assert result.ok
    assert result.order != (3, 1, 3)
    assert result.order[0] <= 2


def test_order_selection_falls_back_on_too_little_history() -> None:
    config = ArimaConfig(auto_order=True, order=(2, 1, 0), min_observations=60)

    assert select_order(random_walk(20), config) == (2, 1, 0)


def test_a_fixed_order_is_honoured() -> None:
    result = fit_forecast(random_walk(300), ArimaConfig(order=(2, 1, 0), auto_order=False))

    assert result.order == (2, 1, 0)


def test_order_selection_is_deterministic() -> None:
    """Same data in, same order out — the reproducibility the phase requires."""
    prices = random_walk(300)
    config = ArimaConfig(auto_order=True, max_p=2, max_q=2)

    assert select_order(prices, config) == select_order(prices, config)


# --- interval width as a trade filter --------------------------------------


def test_interval_width_is_relative_to_price() -> None:
    result = ArimaForecast(
        ok=True, order=(1, 1, 1), predicted=100.0, lower=95.0, upper=105.0, last_price=100.0
    )

    assert result.interval_width == pytest.approx(0.10)


def test_interval_width_is_infinite_without_a_usable_last_price() -> None:
    result = ArimaForecast(ok=True, order=(1, 1, 1), predicted=1.0, lower=0.0, upper=2.0, last_price=0.0)

    assert result.interval_width == float("inf")


def test_a_short_quiet_series_forecasts_more_tightly_than_a_long_wild_one() -> None:
    rng = np.random.default_rng(5)
    quiet = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.0005, 300))), index=hourly_index(300))
    wild = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.05, 300))), index=hourly_index(300))

    assert fit_forecast(quiet).interval_width < fit_forecast(wild).interval_width
