"""Monte Carlo path generators: what each one is supposed to keep.

Choosing a generator is choosing what the result can mean, so the tests
here are mostly about the *statistics that survive* rather than the
shapes of arrays. `docs/analytics/simulation/DESIGN.md` §1 is the table
these assert.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from candlegraph.analytics.simulation import (
    DEFAULT_GENERATOR,
    GENERATOR_NAMES,
    MIN_RETURNS,
    SimulationError,
    generator,
    observed_log_returns,
    simulate_paths,
)

HOURS = 1500


def walk(n: int = 600, sigma: float = 0.01, drift: float = 0.0, seed: int = 5) -> pd.Series:
    """A plain random walk: no clustering, no regimes."""
    rng = np.random.default_rng(seed)
    prices = 100.0 * np.exp(np.cumsum(rng.normal(drift, sigma, n)))
    return pd.Series(prices, index=pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC"), name="WBTC")


def clustered(n: int = HOURS, seed: int = 2) -> pd.Series:
    """A walk whose volatility alternates in long runs.

    Needed because a plain random walk has no clustering to preserve, so
    it cannot tell a block bootstrap from an IID one — the distinction
    the default generator exists for.
    """
    rng = np.random.default_rng(seed)
    volatility = np.where((np.arange(n) // 120) % 2 == 0, 0.003, 0.02)
    prices = 100.0 * np.exp(np.cumsum(rng.normal(0.0, volatility)))
    return pd.Series(prices, index=pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC"), name="X")


def clustering(returns: np.ndarray) -> float:
    """Lag-1 autocorrelation of absolute returns: the clustering measure."""
    magnitude = np.abs(returns)
    return float(np.corrcoef(magnitude[:-1], magnitude[1:])[0, 1])


def path_log_returns(paths: object) -> np.ndarray:
    return np.diff(np.log(paths.prices), axis=1)  # type: ignore[attr-defined]


# --- the registry ------------------------------------------------------------


def test_the_default_generator_is_the_block_bootstrap() -> None:
    """Not IID. An IID bootstrap destroys the volatility clustering most of
    these strategies are implicitly trading, which flatters mean reversion
    and punishes trend following for unrelated reasons."""
    assert DEFAULT_GENERATOR == "block"
    assert GENERATOR_NAMES[0] == "block"


def test_every_generator_says_what_it_destroys() -> None:
    """Choosing one is choosing what the answer can mean."""
    for name in GENERATOR_NAMES:
        rule = generator(name)
        assert rule.preserves and rule.destroys, name


def test_an_unknown_generator_names_the_real_ones() -> None:
    with pytest.raises(SimulationError, match="unknown path generator"):
        generator("crystal-ball")


# --- what each generator preserves -------------------------------------------


@pytest.mark.parametrize("method", GENERATOR_NAMES)
def test_every_generator_reproduces_the_sample_volatility(method: str) -> None:
    prices = walk()
    real = float(np.std(observed_log_returns(prices), ddof=1))

    paths = simulate_paths(prices, n_paths=120, method=method, seed=1)

    assert float(np.std(path_log_returns(paths))) == pytest.approx(real, rel=0.15)


def test_the_block_bootstrap_keeps_volatility_clustering_and_iid_destroys_it() -> None:
    """The entire reason the default is not IID.

    A strategy evaluated against IID paths is being tested on a market
    that has the right return distribution and none of the structure, so
    the comparison answers a different question than the one asked.
    """
    prices = clustered()
    real = clustering(observed_log_returns(prices))
    assert real > 0.2, "fixture must actually cluster, or this proves nothing"

    def measured(method: str) -> float:
        paths = simulate_paths(prices, n_paths=40, method=method, seed=3)
        return float(np.mean([clustering(row) for row in path_log_returns(paths)]))

    assert measured("block") > 0.6 * real, "the block bootstrap must carry clustering through"
    assert abs(measured("iid")) < 0.05, "IID must destroy it"
    assert abs(measured("gbm")) < 0.05, "GBM has no clustering to begin with"


def test_regime_switching_keeps_some_persistence() -> None:
    """A Markov chain over market states, not over parameter space.

    This is the generator half of the terminology trap in the design
    document; MCMC is a different thing entirely and arrives in step 3.
    """
    prices = clustered()
    paths = simulate_paths(prices, n_paths=40, method="regime", seed=3)

    measured = float(np.mean([clustering(row) for row in path_log_returns(paths)]))

    assert measured > 0.1, "regime persistence should show up as clustering"


# --- shape, origin and reproducibility ---------------------------------------


def test_every_path_starts_at_the_real_first_price() -> None:
    prices = walk()

    paths = simulate_paths(prices, n_paths=10, horizon=50, seed=1)

    assert paths.prices.shape == (10, 51), "horizon + 1: the origin is a bar"
    assert np.allclose(paths.prices[:, 0], float(prices.iloc[0]))
    assert (paths.prices > 0).all(), "log space cannot produce a negative price"


def test_the_horizon_defaults_to_the_length_of_the_input() -> None:
    """So a simulated backtest is comparable with the real one."""
    prices = walk(n=200)

    paths = simulate_paths(prices, n_paths=4, seed=1)

    assert paths.prices.shape[1] == len(prices.dropna())


def test_the_same_seed_gives_the_same_paths_and_a_different_one_does_not() -> None:
    """A distribution that changes between two readings is not evidence."""
    prices = walk()

    first = simulate_paths(prices, n_paths=8, seed=42)
    again = simulate_paths(prices, n_paths=8, seed=42)
    other = simulate_paths(prices, n_paths=8, seed=43)

    assert np.array_equal(first.prices, again.prices)
    assert not np.array_equal(first.prices, other.prices)
    assert first.seed == 42, "the seed travels with the result"


def test_the_result_remembers_how_little_it_was_built_from() -> None:
    """Ten thousand paths from ninety bars is still ninety bars."""
    prices = walk(n=120)

    paths = simulate_paths(prices, n_paths=5000, seed=1)

    assert paths.source_bars == 119
    assert paths.n_paths == 5000


# --- refusals ----------------------------------------------------------------


def test_too_little_history_is_refused_rather_than_resampled() -> None:
    """Below this, every path is a rearrangement of the same few moves."""
    with pytest.raises(SimulationError, match="usable returns"):
        simulate_paths(walk(n=MIN_RETURNS - 5), n_paths=10)


def test_nonsense_counts_are_refused() -> None:
    prices = walk()
    with pytest.raises(SimulationError, match="n_paths"):
        simulate_paths(prices, n_paths=0)
    with pytest.raises(SimulationError, match="horizon"):
        simulate_paths(prices, n_paths=2, horizon=0)


# --- the fan -----------------------------------------------------------------


def test_the_fan_is_ordered_and_spans_the_horizon() -> None:
    """A thousand lines are noise; the envelope is the statement."""
    paths = simulate_paths(walk(), n_paths=200, horizon=40, seed=1)

    fan = paths.fan()

    assert list(fan.columns) == ["p5", "p25", "p50", "p75", "p95"]
    assert len(fan) == 41
    for lower, upper in zip(fan.columns[:-1], fan.columns[1:], strict=True):
        assert (fan[lower] <= fan[upper] + 1e-9).all(), f"{lower} must not exceed {upper}"
    # Every path starts at the same price, so the fan opens at a point.
    assert fan.iloc[0].nunique() == 1
    assert fan["p95"].iloc[-1] > fan["p5"].iloc[-1], "uncertainty grows with horizon"
