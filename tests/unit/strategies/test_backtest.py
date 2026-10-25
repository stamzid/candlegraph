"""The backtester, and above all the execution lag that keeps it honest."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from candlegraph.strategies.backtest import (
    BacktestConfig,
    BacktestError,
    backtest,
    compare,
    format_backtest,
)
from candlegraph.strategies.base import StrategyResult
from tests.unit.analytics.conftest import hourly_index


def result_from(prices: list[float], signals: list[int], name: str = "test") -> StrategyResult:
    index = hourly_index(len(prices))
    frame = pd.DataFrame({"price": prices, "signal": signals, "score": np.zeros(len(prices))}, index=index)
    return StrategyResult(name=name, symbol="WETH", frame=frame)


FREE = BacktestConfig(fee_bps=0.0, slippage_bps=0.0)


# --- configuration ---------------------------------------------------------


@pytest.mark.parametrize("field", ["fee_bps", "slippage_bps"])
def test_negative_costs_are_rejected(field: str) -> None:
    with pytest.raises(BacktestError, match="must not be negative"):
        BacktestConfig(**{field: -1.0})


def test_zero_capital_is_rejected() -> None:
    with pytest.raises(BacktestError, match="initial_capital"):
        BacktestConfig(initial_capital=0.0)


def test_costs_combine_fees_and_slippage() -> None:
    assert BacktestConfig(fee_bps=30.0, slippage_bps=5.0).cost_per_unit == pytest.approx(0.0035)


# --- the execution lag -----------------------------------------------------


def test_a_position_earns_the_next_bars_return_not_this_ones() -> None:
    """The single most important behaviour in the module.

    The signal fires on bar 1 and the price jumps between bars 1 and 2, so
    the gain must land on bar 2. Crediting it to bar 1 would mean trading at
    a price we had not yet seen.
    """
    result = result_from([100.0, 100.0, 110.0, 110.0], [0, 1, 1, 1])

    frame = backtest(result, config=FREE).frame

    assert frame["position"].iloc[1] == 0.0
    assert frame["position"].iloc[2] == 1.0
    assert frame["strategy_returns"].iloc[1] == pytest.approx(0.0)
    assert frame["strategy_returns"].iloc[2] == pytest.approx(0.10)


def test_a_signal_on_the_final_bar_earns_nothing() -> None:
    """There is no next bar to trade into."""
    result = result_from([100.0, 100.0, 100.0, 200.0], [0, 0, 0, 1])

    assert backtest(result, config=FREE).total_return == pytest.approx(0.0)


def test_removing_the_lag_manufactures_a_profit() -> None:
    """Demonstrates the bias the default guards against.

    With perfect foresight and no lag the same signals look brilliant; with
    the honest one-bar lag they earn nothing. A backtest that silently used
    lag 0 would report the first number.
    """
    # Returns are [-, +10%, -9.1%, +10%]. These signals are long exactly on
    # the up bars, which is only knowable after the fact.
    prices = [100.0, 110.0, 100.0, 110.0]
    signals = [0, 1, 0, 1]

    honest = backtest(result_from(prices, signals), config=FREE)
    cheating = backtest(
        result_from(prices, signals), config=BacktestConfig(fee_bps=0, slippage_bps=0, execution_lag=0)
    )

    assert cheating.total_return > honest.total_return


# --- returns and costs -----------------------------------------------------


def test_a_flat_strategy_returns_nothing_and_pays_nothing() -> None:
    result = backtest(result_from([100.0, 120.0, 90.0], [0, 0, 0]), config=FREE)

    assert result.total_return == pytest.approx(0.0)
    assert result.total_costs == pytest.approx(0.0)
    assert result.trades == 0


def test_an_always_long_strategy_tracks_buy_and_hold() -> None:
    prices = [100.0, 110.0, 121.0, 133.1]
    result = backtest(result_from(prices, [1, 1, 1, 1]), config=FREE)

    # One bar of lag means it misses only the very first move.
    assert result.total_return == pytest.approx(0.331, rel=1e-3)
    assert result.buy_and_hold_return == pytest.approx(0.331, rel=1e-3)


def test_a_short_position_profits_from_a_fall() -> None:
    result = backtest(result_from([100.0, 100.0, 90.0], [0, -1, -1]), config=FREE)

    assert result.total_return == pytest.approx(0.10)


def test_costs_are_charged_when_the_position_changes() -> None:
    config = BacktestConfig(fee_bps=50.0, slippage_bps=0.0)

    result = backtest(result_from([100.0, 100.0, 100.0], [0, 1, 1]), config=config)

    # One unit of turnover at 50bps.
    assert result.total_costs == pytest.approx(0.005)
    assert result.total_return == pytest.approx(-0.005)


def test_reversing_a_position_costs_twice_what_closing_it_does() -> None:
    """Long to short moves two units of exposure; long to flat moves one."""
    config = BacktestConfig(fee_bps=50.0, slippage_bps=0.0)
    unit = 0.005

    closing = backtest(result_from([100.0] * 4, [0, 1, 0, 0]), config=config)
    reversing = backtest(result_from([100.0] * 4, [0, 1, -1, -1]), config=config)

    # Both pay one unit to open; then one to close versus two to reverse.
    assert closing.total_costs == pytest.approx(2 * unit)
    assert reversing.total_costs == pytest.approx(3 * unit)


def test_costs_reduce_the_net_return_below_the_gross() -> None:
    result = backtest(result_from([100.0, 100.0, 110.0], [0, 1, 1]), config=BacktestConfig(fee_bps=100.0))

    assert result.frame["gross_returns"].sum() > result.frame["strategy_returns"].sum()


# --- metrics ---------------------------------------------------------------


def test_excess_return_compares_against_holding() -> None:
    result = backtest(result_from([100.0, 110.0, 121.0], [0, 0, 0]), config=FREE)

    assert result.total_return == pytest.approx(0.0)
    assert result.buy_and_hold_return > 0
    assert result.excess_return == pytest.approx(-result.buy_and_hold_return)


def test_exposure_is_the_fraction_of_bars_in_the_market() -> None:
    result = backtest(result_from([100.0] * 5, [1, 1, 0, 0, 0]), config=FREE)

    # Lagged: positions are 0,1,1,0,0.
    assert result.exposure == pytest.approx(0.4)


def test_trades_counts_executed_position_changes_not_signal_changes() -> None:
    """The final signal change has no bar left to execute in, so it is not a trade."""
    result = backtest(result_from([100.0] * 6, [0, 1, 1, 0, 1, 0]), config=FREE)

    # Signals change 4 times; positions (lagged) change 3 times.
    assert result.trades == 3


def test_the_win_rate_only_counts_bars_with_a_position() -> None:
    result = backtest(result_from([100.0, 100.0, 110.0, 105.0], [0, 1, 1, 1]), config=FREE)

    # Two bars held: one up, one down.
    assert result.win_rate == pytest.approx(0.5)


def test_drawdown_is_reported_as_a_non_positive_fraction() -> None:
    result = backtest(result_from([100.0, 100.0, 50.0, 60.0], [0, 1, 1, 1]), config=FREE)

    assert result.max_drawdown < 0
    assert result.frame["drawdown"].le(0).all()


def test_the_equity_curve_starts_at_the_configured_capital() -> None:
    config = BacktestConfig(fee_bps=0, slippage_bps=0, initial_capital=5_000.0)

    result = backtest(result_from([100.0, 110.0], [0, 0]), config=config)

    assert result.equity.iloc[0] == pytest.approx(5_000.0)


def test_sharpe_responds_to_the_risk_free_rate() -> None:
    prices = list(100 * np.cumprod(1 + np.tile([0.01, -0.005], 100)))
    signals = [1] * len(prices)

    cheap = backtest(result_from(prices, signals), config=FREE)
    dear = backtest(
        result_from(prices, signals),
        config=BacktestConfig(fee_bps=0, slippage_bps=0, risk_free_rate=0.5),
    )

    assert dear.sharpe < cheap.sharpe


def test_a_total_wipeout_does_not_produce_a_nan_annualised_return() -> None:
    """``growth ** (factor / bars)`` is undefined for a non-positive base."""
    result = backtest(result_from([100.0, 100.0, 0.0001], [0, 1, 1]), config=FREE)

    assert np.isfinite(result.annualised_return)


# --- plumbing --------------------------------------------------------------


def test_prices_can_be_supplied_separately() -> None:
    index = hourly_index(3)
    frame = pd.DataFrame({"signal": [0, 1, 1], "score": [0.0] * 3}, index=index)
    result = StrategyResult(name="bare", symbol="WETH", frame=frame)

    marked = backtest(result, prices=pd.Series([100.0, 100.0, 110.0], index=index), config=FREE)

    assert marked.total_return == pytest.approx(0.10)


def test_a_strategy_with_no_price_and_no_override_is_an_error() -> None:
    frame = pd.DataFrame({"signal": [0], "score": [0.0]}, index=hourly_index(1))
    result = StrategyResult(name="bare", symbol="WETH", frame=frame)

    with pytest.raises(BacktestError, match="no 'price' column"):
        backtest(result)


def test_strategy_params_are_carried_into_the_result() -> None:
    frame = pd.DataFrame({"price": [1.0], "signal": [0], "score": [0.0]}, index=hourly_index(1))
    result = StrategyResult(name="x", symbol="WETH", frame=frame, params={"window": 20})

    assert backtest(result, config=FREE).params == {"window": 20}


# --- reporting -------------------------------------------------------------


def test_the_report_names_the_strategy_and_the_costs() -> None:
    result = backtest(result_from([100.0, 110.0], [0, 1]), config=BacktestConfig(fee_bps=30.0))

    text = format_backtest(result)

    assert "test on WETH" in text
    assert "30bps" in text
    assert "execution lag 1" in text
    assert "sharpe" in text


def test_the_report_lists_params_when_present() -> None:
    frame = pd.DataFrame({"price": [1.0, 1.0], "signal": [0, 0], "score": [0.0, 0.0]}, index=hourly_index(2))
    result = StrategyResult(name="x", symbol="WETH", frame=frame, params={"window": 20})

    assert "window=20" in format_backtest(backtest(result, config=FREE))


def test_compare_tabulates_several_runs() -> None:
    a = backtest(result_from([100.0, 110.0], [0, 1], name="a"), config=FREE)
    b = backtest(result_from([100.0, 110.0], [0, 0], name="b"), config=FREE)

    table = compare([a, b])

    assert list(table["strategy"]) == ["a", "b"]
    assert {"sharpe", "max_drawdown", "excess"} <= set(table.columns)
