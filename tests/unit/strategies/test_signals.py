"""The three strategies, the registry, and persisting what they emit."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from candlegraph.analytics.arima import ArimaConfig
from candlegraph.strategies import available, get, run_strategy
from candlegraph.strategies.arima_signal import ArimaSignalConfig, ArimaSignalError, arima_signal
from candlegraph.strategies.base import Signal, StrategyResult, params_of
from candlegraph.strategies.bollinger import (
    BollingerConfig,
    BollingerError,
    bollinger_mean_reversion,
)
from candlegraph.strategies.crossover import (
    CrossoverConfig,
    CrossoverError,
    moving_average_crossover,
)
from candlegraph.strategies.store import signals_to_rows, store_signals
from tests.conftest import FakeDatabase
from tests.unit.analytics.conftest import hourly_index, linear_series, random_walk

NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)


def series(values: list[float], name: str = "WETH") -> pd.Series:
    return pd.Series(values, index=hourly_index(len(values)), name=name, dtype="float64")


# --- the registry ----------------------------------------------------------


def test_every_built_in_strategy_is_registered() -> None:
    assert available() == ("arima", "bollinger", "crossover")


def test_a_registration_carries_its_config_type() -> None:
    assert get("bollinger").config_type is BollingerConfig
    assert get("crossover").config_type is CrossoverConfig
    assert get("arima").config_type is ArimaSignalConfig


def test_every_registration_describes_itself() -> None:
    assert all(get(name).description for name in available())


def test_an_unknown_strategy_lists_the_alternatives() -> None:
    with pytest.raises(KeyError, match="bollinger"):
        get("nope")


def test_a_strategy_can_be_run_by_name() -> None:
    result = run_strategy("bollinger", random_walk(200))

    assert isinstance(result, StrategyResult)
    assert result.name == "bollinger"


def test_params_of_a_dataclass_is_json_safe() -> None:
    params = params_of(ArimaSignalConfig())

    json.dumps(params, default=str)
    assert params["threshold"] == 0.002


# --- the result contract ---------------------------------------------------


def test_a_result_without_the_required_columns_is_rejected() -> None:
    frame = pd.DataFrame({"price": [1.0]}, index=hourly_index(1))

    with pytest.raises(ValueError, match="no \\['signal', 'score'\\]"):
        StrategyResult(name="x", symbol="WETH", frame=frame)


def test_latest_is_flat_for_an_empty_result() -> None:
    frame = pd.DataFrame({"signal": [], "score": []}, index=hourly_index(0))

    assert StrategyResult(name="x", symbol="W", frame=frame).latest is Signal.FLAT


@pytest.mark.parametrize("name", ["bollinger", "crossover"])
def test_every_strategy_emits_integer_signals_in_range(name: str) -> None:
    result = run_strategy(name, random_walk(400))

    assert set(result.signals.unique()) <= {-1, 0, 1}


@pytest.mark.parametrize("name", ["bollinger", "crossover"])
def test_every_strategy_aligns_its_output_to_the_input(name: str) -> None:
    prices = random_walk(300)

    result = run_strategy(name, prices)

    assert len(result.frame) == len(prices)
    assert result.frame.index.equals(prices.index)


@pytest.mark.parametrize("name", ["bollinger", "crossover"])
def test_every_strategy_keeps_the_symbol(name: str) -> None:
    assert run_strategy(name, random_walk(300).rename("WBTC")).symbol == "WBTC"


# --- Bollinger -------------------------------------------------------------


def test_bollinger_rejects_an_exit_above_its_entry() -> None:
    """Without the gap the position thrashes once per bar on noise."""
    with pytest.raises(BollingerError, match="must be below entry_z"):
        BollingerConfig(entry_z=1.0, exit_z=1.5)


@pytest.mark.parametrize("kwargs", [{"window": 1}, {"num_std": 0.0}, {"entry_z": 0.0}, {"exit_z": -1.0}])
def test_bollinger_rejects_degenerate_parameters(kwargs: dict) -> None:
    with pytest.raises(BollingerError):
        BollingerConfig(**kwargs)


def test_bollinger_is_flat_before_its_first_full_window() -> None:
    result = bollinger_mean_reversion(random_walk(100), BollingerConfig(window=20))

    assert (result.signals.iloc[:19] == 0).all()


def test_bollinger_goes_long_when_price_collapses_below_the_band() -> None:
    prices = series([100.0] * 25 + [80.0])

    result = bollinger_mean_reversion(prices, BollingerConfig(window=20, entry_z=1.5, exit_z=0.5))

    assert result.signals.iloc[-1] == Signal.LONG


def test_bollinger_goes_short_when_price_spikes_above_the_band() -> None:
    prices = series([100.0] * 25 + [120.0])

    result = bollinger_mean_reversion(prices, BollingerConfig(window=20, entry_z=1.5, exit_z=0.5))

    assert result.signals.iloc[-1] == Signal.SHORT


def test_bollinger_stays_flat_on_a_spike_when_shorting_is_disabled() -> None:
    prices = series([100.0] * 25 + [120.0])

    result = bollinger_mean_reversion(
        prices, BollingerConfig(window=20, entry_z=1.5, exit_z=0.5, allow_short=False)
    )

    assert result.signals.iloc[-1] == Signal.FLAT


def test_bollinger_holds_through_the_hysteresis_band() -> None:
    """Entered at z <= -2; a drift back to z = -1 is not yet an exit."""
    prices = series([100.0] * 22 + [90.0, 97.0])
    config = BollingerConfig(window=20, entry_z=1.5, exit_z=0.2)

    signals = bollinger_mean_reversion(prices, config).signals

    assert signals.iloc[-2] == Signal.LONG
    assert signals.iloc[-1] == Signal.LONG


def test_a_wider_entry_threshold_trades_less() -> None:
    prices = random_walk(600)

    eager = bollinger_mean_reversion(prices, BollingerConfig(entry_z=1.0, exit_z=0.2))
    patient = bollinger_mean_reversion(prices, BollingerConfig(entry_z=3.0, exit_z=0.2))

    assert patient.trades() <= eager.trades()


def test_bollinger_exposes_the_zscore_as_its_score() -> None:
    result = bollinger_mean_reversion(random_walk(300))

    pd.testing.assert_series_equal(result.frame["score"], result.frame["zscore"], check_names=False)


def test_bollinger_records_its_configuration() -> None:
    result = bollinger_mean_reversion(random_walk(300), BollingerConfig(window=30, entry_z=2.5))

    assert result.params["window"] == 30
    assert result.params["entry_z"] == 2.5


# --- crossover -------------------------------------------------------------


def test_crossover_needs_a_slow_window_above_the_fast_one() -> None:
    with pytest.raises(CrossoverError, match="must exceed fast"):
        CrossoverConfig(fast=20, slow=10)


def test_crossover_is_long_in_an_uptrend() -> None:
    result = moving_average_crossover(linear_series(200), CrossoverConfig(fast=5, slow=20))

    assert result.signals.iloc[-1] == Signal.LONG


def test_crossover_is_flat_before_the_slow_window_fills() -> None:
    result = moving_average_crossover(linear_series(200), CrossoverConfig(fast=5, slow=20))

    assert (result.signals.iloc[:19] == 0).all()


def test_crossover_shorts_a_downtrend_only_when_allowed() -> None:
    falling = series(list(np.linspace(200.0, 100.0, 200)))

    long_only = moving_average_crossover(falling, CrossoverConfig(fast=5, slow=20))
    both = moving_average_crossover(falling, CrossoverConfig(fast=5, slow=20, allow_short=True))

    assert long_only.signals.iloc[-1] == Signal.FLAT
    assert both.signals.iloc[-1] == Signal.SHORT


def test_a_separation_filter_suppresses_marginal_crossings() -> None:
    prices = random_walk(500)

    noisy = moving_average_crossover(prices, CrossoverConfig(fast=5, slow=20))
    filtered = moving_average_crossover(prices, CrossoverConfig(fast=5, slow=20, min_separation=0.02))

    assert filtered.trades() <= noisy.trades()


# --- ARIMA signal ----------------------------------------------------------


def test_arima_signal_rejects_a_warmup_below_the_models_minimum() -> None:
    with pytest.raises(ArimaSignalError, match="below the model's min_observations"):
        ArimaSignalConfig(arima=ArimaConfig(min_observations=100), warmup=50)


@pytest.mark.parametrize("kwargs", [{"threshold": -0.1}, {"refit_every": 0}, {"max_interval_width": 0}])
def test_arima_signal_rejects_degenerate_parameters(kwargs: dict) -> None:
    with pytest.raises(ArimaSignalError):
        ArimaSignalConfig(**kwargs)


def test_arima_is_flat_through_its_warmup() -> None:
    config = ArimaSignalConfig(arima=ArimaConfig(min_observations=60), refit_every=20)

    result = arima_signal(random_walk(150), config)

    assert (result.signals.iloc[:60] == 0).all()


def test_arima_only_refits_on_its_cadence() -> None:
    """Refitting dominates runtime, so the cadence has to actually apply."""
    config = ArimaSignalConfig(arima=ArimaConfig(min_observations=60), refit_every=10)

    result = arima_signal(random_walk(150), config)

    refits = int(result.frame["refit"].sum())
    assert refits == len(range(60, 150, 10))


def test_arima_holds_its_position_between_refits() -> None:
    config = ArimaSignalConfig(arima=ArimaConfig(min_observations=60), refit_every=25, threshold=0.0)

    result = arima_signal(random_walk(130), config)
    held = result.signals.iloc[60:85]

    assert held.nunique() == 1


def test_a_higher_threshold_takes_fewer_positions() -> None:
    prices = random_walk(200)
    base = ArimaConfig(min_observations=60)

    eager = arima_signal(prices, ArimaSignalConfig(arima=base, threshold=0.0, refit_every=10))
    strict = arima_signal(prices, ArimaSignalConfig(arima=base, threshold=0.5, refit_every=10))

    assert strict.exposure() <= eager.exposure()
    assert strict.exposure() == 0.0


def test_a_tight_interval_filter_stands_the_strategy_aside() -> None:
    """A forecast whose interval spans 1% of price is not worth paying fees for."""
    prices = random_walk(200)
    base = ArimaConfig(min_observations=60)

    permissive = arima_signal(
        prices, ArimaSignalConfig(arima=base, threshold=0.0, refit_every=20, max_interval_width=None)
    )
    fussy = arima_signal(
        prices,
        ArimaSignalConfig(arima=base, threshold=0.0, refit_every=20, max_interval_width=1e-6),
    )

    assert fussy.exposure() == 0.0
    assert permissive.exposure() > 0.0


def test_arima_shorts_only_when_allowed() -> None:
    prices = random_walk(200)
    base = ArimaConfig(min_observations=60)

    both = arima_signal(prices, ArimaSignalConfig(arima=base, threshold=0.0, refit_every=10))
    long_only = arima_signal(
        prices, ArimaSignalConfig(arima=base, threshold=0.0, refit_every=10, allow_short=False)
    )

    assert (long_only.signals >= 0).all()
    assert set(both.signals.unique()) <= {-1, 0, 1}


def test_arima_exposes_the_forecast_return_as_its_score() -> None:
    config = ArimaSignalConfig(arima=ArimaConfig(min_observations=60), refit_every=20)

    frame = arima_signal(random_walk(150), config).frame

    assert "forecast" in frame
    assert "interval_width" in frame
    assert frame["score"].notna().any()


def test_a_series_too_short_to_fit_yields_no_positions() -> None:
    config = ArimaSignalConfig(arima=ArimaConfig(min_observations=60))

    result = arima_signal(random_walk(30), config)

    assert (result.signals == 0).all()


def test_arima_never_fits_on_data_after_the_bar_it_scores() -> None:
    """The look-ahead guard: appending future bars must not change the past.

    If the walk-forward loop ever sliced the whole series, extending the data
    would silently rewrite earlier signals.
    """
    prices = random_walk(200)
    config = ArimaSignalConfig(arima=ArimaConfig(min_observations=60), refit_every=10, threshold=0.0)

    short_run = arima_signal(prices.iloc[:150], config)
    long_run = arima_signal(prices, config)

    pd.testing.assert_series_equal(
        short_run.signals.iloc[:150], long_run.signals.iloc[:150], check_freq=False
    )


# --- storing signals -------------------------------------------------------


def test_rows_carry_the_natural_key_and_the_params() -> None:
    result = bollinger_mean_reversion(random_walk(60).rename("WETH"))

    rows = signals_to_rows(result, generated_at=NOW)

    assert len(rows) == 60
    first = rows[0]
    assert first["strategy"] == "bollinger"
    assert first["symbol"] == "WETH"
    assert first["generated_at"] == NOW
    assert json.loads(first["params"])["window"] == 20


def test_a_nan_score_is_stored_as_null_not_zero() -> None:
    """Before the warm-up there is no opinion, which is not an opinion of zero."""
    result = bollinger_mean_reversion(random_walk(60).rename("WETH"), BollingerConfig(window=20))

    rows = signals_to_rows(result, generated_at=NOW)

    assert rows[0]["score"] is None
    assert rows[-1]["score"] is not None


def test_flat_bars_can_be_excluded() -> None:
    result = bollinger_mean_reversion(random_walk(300).rename("WETH"))

    everything = signals_to_rows(result, generated_at=NOW)
    only_positions = signals_to_rows(result, generated_at=NOW, include_flat=False)

    assert len(only_positions) < len(everything)
    assert all(row["signal"] != 0 for row in only_positions)


def test_the_period_key_matches_the_bar_timestamp() -> None:
    result = bollinger_mean_reversion(random_walk(60).rename("WETH"))

    row = signals_to_rows(result, generated_at=NOW)[0]

    assert row["period_start_unix"] == int(row["timestamp"].timestamp())


def test_an_unnamed_series_needs_an_explicit_symbol() -> None:
    result = bollinger_mean_reversion(pd.Series([1.0] * 60, index=hourly_index(60)))

    with pytest.raises(ValueError, match="without a symbol"):
        signals_to_rows(result)

    assert signals_to_rows(result, symbol="WETH")[0]["symbol"] == "WETH"


def test_storing_upserts_through_the_signal_statement() -> None:
    db = FakeDatabase()
    result = bollinger_mean_reversion(random_walk(60).rename("WETH"))

    written = store_signals(db, result, generated_at=NOW)

    assert written == 60
    [(label, rows)] = db.upserts
    assert label == "insert_signal"
    assert len(rows) == 60


def test_storing_nothing_writes_nothing() -> None:
    db = FakeDatabase()
    frame = pd.DataFrame({"signal": [], "score": []}, index=hourly_index(0))

    assert store_signals(db, StrategyResult("x", "WETH", frame)) == 0
    assert db.upserts == []
