"""Running a strategy over synthetic markets: is the edge luck?

The harness calls the real backtester, so what is tested here is the
*framing*: that the distribution is built from many independent paths,
that the p-value answers the question it claims to, and that the result
carries enough provenance (seed, generator, source bars) to be evidence
rather than decoration.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from candlegraph.analytics.simulation import SimulationError
from candlegraph.strategies.backtest import BacktestConfig
from candlegraph.strategies.montecarlo import (
    SAMPLE_CAVEAT,
    TRACKED,
    evaluate_strategy,
    format_simulation,
)

FREE = BacktestConfig(fee_bps=0.0, slippage_bps=0.0)


def walk(n: int = 400, seed: int = 5, drift: float = 0.0) -> pd.Series:
    rng = np.random.default_rng(seed)
    prices = 100.0 * np.exp(np.cumsum(rng.normal(drift, 0.01, n)))
    return pd.Series(prices, index=pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC"), name="WBTC")


def summary(paths: int = 60, **kwargs: object) -> object:
    return evaluate_strategy(walk(), "bollinger", n_paths=paths, seed=3, config=FREE, **kwargs)  # type: ignore[arg-type]


# --- the distribution ---------------------------------------------------------


def test_every_path_is_backtested_and_tracked() -> None:
    result = summary(paths=40)

    assert result.n_paths == 40  # type: ignore[attr-defined]
    assert list(result.outcomes.columns) == list(TRACKED)  # type: ignore[attr-defined]
    assert result.outcomes["excess_return"].nunique() > 1, "paths must differ from each other"  # type: ignore[attr-defined]


def test_the_real_backtest_is_kept_beside_the_distribution() -> None:
    """The comparison is the whole point; losing the actual result would
    leave a distribution with nothing to be a distribution *about*."""
    result = summary()

    assert result.actual.strategy == "bollinger"  # type: ignore[attr-defined]
    assert result.actual.bars > 0  # type: ignore[attr-defined]


def test_the_result_carries_the_seed_and_the_generator() -> None:
    """A distribution that changes between two readings is not evidence."""
    first = summary()
    again = summary()

    assert first.seed == 3 and first.method == "block"  # type: ignore[attr-defined]
    assert first.outcomes.equals(again.outcomes)  # type: ignore[attr-defined]


def test_a_different_seed_gives_a_different_distribution() -> None:
    first = evaluate_strategy(walk(), "bollinger", n_paths=40, seed=1, config=FREE)
    other = evaluate_strategy(walk(), "bollinger", n_paths=40, seed=2, config=FREE)

    assert not first.outcomes.equals(other.outcomes)


def test_the_result_remembers_how_little_it_came_from() -> None:
    """Ten thousand paths from ninety bars is still ninety bars."""
    result = summary()

    assert result.source_bars == 399  # type: ignore[attr-defined]
    assert result.caveat == SAMPLE_CAVEAT  # type: ignore[attr-defined]


# --- the p-value ---------------------------------------------------------------


def test_the_p_value_is_the_share_of_paths_that_did_as_well() -> None:
    result = summary(paths=50)

    simulated = result.outcomes["excess_return"].to_numpy()  # type: ignore[attr-defined]
    beat = int(np.sum(simulated >= result.actual.excess_return))  # type: ignore[attr-defined]

    assert result.p_value() == pytest.approx((beat + 1) / 51)  # type: ignore[attr-defined]
    assert 0.0 < result.p_value() <= 1.0  # type: ignore[attr-defined]


def test_the_p_value_is_never_exactly_zero() -> None:
    """A thousand paths cannot evidence one-in-a-million; reporting 0.000
    would claim more than the sample size supports."""
    result = summary(paths=30)

    assert result.p_value() >= 1 / 31  # type: ignore[attr-defined]


def test_a_strategy_with_no_edge_is_reported_as_having_none() -> None:
    """Bollinger on a random walk is the canonical no-edge case, and the
    verdict has to say so rather than hedging."""
    result = summary(paths=100)

    assert result.p_value() > 0.10  # type: ignore[attr-defined]
    assert "No evidence of an edge" in result.verdict  # type: ignore[attr-defined]


def test_an_unknown_metric_is_refused() -> None:
    with pytest.raises(SimulationError, match="no metric"):
        summary().p_value("vibes")  # type: ignore[attr-defined]


def test_percentiles_are_ordered() -> None:
    bands = summary().percentiles()  # type: ignore[attr-defined]

    values = [bands[name] for name in ("p5", "p25", "p50", "p75", "p95")]
    assert values == sorted(values)


# --- refusals and reporting -----------------------------------------------------


def test_an_unknown_generator_is_refused_before_any_work() -> None:
    with pytest.raises(SimulationError, match="unknown path generator"):
        evaluate_strategy(walk(), "bollinger", n_paths=5, method="crystal-ball", config=FREE)


def test_too_little_history_is_refused() -> None:
    with pytest.raises(SimulationError, match="usable returns"):
        evaluate_strategy(walk(n=20), "bollinger", n_paths=5, config=FREE)


def test_the_printed_report_states_the_seed_the_caveat_and_the_verdict() -> None:
    text = format_simulation(summary(paths=40))  # type: ignore[arg-type]

    assert "seed 3" in text and "generator 'block'" in text
    assert "p-value" in text
    assert "still ninety days" in text, "the caveat travels with every surface"
    assert "399 real bars" in text
