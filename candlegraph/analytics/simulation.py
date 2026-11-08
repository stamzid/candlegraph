"""Monte Carlo path generators: markets that did not happen.

Pure numerics, like the rest of :mod:`candlegraph.analytics`. A price
series in, a matrix of synthetic price paths out; no database, no
strategies, no I/O. The harness that runs a *strategy* over these paths
lives in :mod:`candlegraph.strategies.montecarlo`, because it needs the
backtester and ``strategies`` already imports ``analytics`` — putting it
here would close an import cycle.

``docs/analytics/simulation/DESIGN.md`` §1 compares the generators and
says which to reach for. Two things from it are load-bearing here:

* **The default is the stationary block bootstrap.** An IID bootstrap
  destroys volatility clustering, which most of these strategies are
  implicitly trading. It flatters mean reversion and punishes trend
  following for reasons that have nothing to do with either, so it is a
  *null hypothesis* generator rather than a realistic one.
* **A bootstrap of a bull sample simulates bull markets.** Resampling
  widens the sample along one axis only — the ordering and recombination
  of moves that actually happened. Ten thousand paths from ninety days is
  still ninety days, and every surface that reports a distribution says
  so.

Everything is generated in **log space** and exponentiated at the end: a
random walk in logs cannot produce a negative price, and a 1% move costs
the same at 3 as at 30,000.

Every generator takes a seed and the seed travels with the result. A
distribution that changes between two readings of the same page is
decoration, not evidence.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd

from candlegraph.analytics.rolling import log_returns

# Below this many observed returns, resampling is self-portraiture: every
# path is a rearrangement of the same handful of moves.
MIN_RETURNS = 30
# Mean block length for the stationary bootstrap, in bars. One day of
# hourly data: long enough to carry a volatility cluster, short enough
# that a 90-day sample still yields many distinct blocks.
DEFAULT_BLOCK = 24
# Rolling window used to label volatility regimes.
REGIME_WINDOW = 24


class SimulationError(ValueError):
    """The simulation cannot be run as asked, phrased for whoever asked."""


@dataclass(frozen=True)
class PathGenerator:
    """One way of inventing a price path, and what it costs.

    ``preserves`` and ``destroys`` are the honest summary from the design
    document's comparison table; they are shown next to the picker,
    because choosing a generator *is* choosing what the result can mean.
    """

    name: str
    summary: str
    preserves: str
    destroys: str


GENERATORS: dict[str, PathGenerator] = {
    "block": PathGenerator(
        "block",
        "Stationary block bootstrap: resample runs of consecutive real returns.",
        preserves="short-range dependence and volatility clustering, within the block length",
        destroys="structure longer than a block",
    ),
    "iid": PathGenerator(
        "iid",
        "Shuffle the real returns independently.",
        preserves="the return distribution, fat tails included",
        destroys="all autocorrelation and volatility clustering",
    ),
    "gbm": PathGenerator(
        "gbm",
        "Geometric Brownian motion at the sample's own drift and volatility.",
        preserves="drift and volatility",
        destroys="fat tails, clustering, mean reversion",
    ),
    "regime": PathGenerator(
        "regime",
        "Two-state Markov regime switching: calm and volatile, each with its own moments.",
        preserves="regime persistence and regime-dependent volatility",
        destroys="structure within a regime",
    ),
}
# Presentation order, cheapest assumption first.
GENERATOR_NAMES: tuple[str, ...] = ("block", "iid", "gbm", "regime")

DEFAULT_GENERATOR = "block"


def generator(name: str) -> PathGenerator:
    """The generator named ``name``, or an error listing the real ones."""
    try:
        return GENERATORS[name]
    except KeyError:
        raise SimulationError(
            f"unknown path generator {name!r}; known: {', '.join(GENERATOR_NAMES)}"
        ) from None


@dataclass(frozen=True)
class SimulatedPaths:
    """Synthetic price paths, and everything needed to reproduce them."""

    prices: np.ndarray
    """``(n_paths, horizon + 1)``. Column 0 is ``start_price`` on every path."""
    method: str
    seed: int
    start_price: float
    source_bars: int
    """How many real returns the paths were built from. The honesty number:
    a thousand paths from ninety bars is still ninety bars."""

    @property
    def n_paths(self) -> int:
        return int(self.prices.shape[0])

    @property
    def horizon(self) -> int:
        return int(self.prices.shape[1] - 1)

    def path(self, index: int) -> np.ndarray:
        return np.asarray(self.prices[index], dtype="float64")

    def terminal_returns(self) -> np.ndarray:
        """Total return of each path, as a fraction."""
        return self.prices[:, -1] / self.start_price - 1.0

    def fan(self, levels: tuple[float, ...] = (5, 25, 50, 75, 95)) -> pd.DataFrame:
        """Percentile bands across paths, one column per level.

        A fan is the honest way to draw a thousand paths: the individual
        lines are noise, the envelope is the statement.
        """
        bands = np.percentile(self.prices, levels, axis=0)
        return pd.DataFrame(
            {f"p{int(level)}": band for level, band in zip(levels, bands, strict=True)},
            index=pd.RangeIndex(self.horizon + 1, name="bar"),
        )


# --- the generators, one formula each ----------------------------------------


def _draw_iid(returns: np.ndarray, shape: tuple[int, int], rng: np.random.Generator) -> np.ndarray:
    """Resample returns independently with replacement.

    No time structure survives, which is the point: this is the null
    hypothesis a strategy's edge has to beat.
    """
    return rng.choice(returns, size=shape, replace=True)


def _block_start_indices(
    shape: tuple[int, int], source: int, rng: np.random.Generator, mean_block: int
) -> np.ndarray:
    """Index matrix for the stationary bootstrap (Politis-Romano).

    Each step either continues the current block (advancing one position,
    wrapping at the end of the sample) or starts a new block at a uniformly
    random point. Block lengths are geometric with mean ``mean_block``, so
    there is no fixed block boundary for the sample to line up with.

    Written as an index matrix rather than a loop because the sequential
    version is a few million Python iterations at realistic path counts.
    """
    _, horizon = shape
    restart_probability = 1.0 / max(mean_block, 1)

    starts_a_block = rng.random(shape) < restart_probability
    starts_a_block[:, 0] = True  # every path begins one
    candidate_starts = rng.integers(0, source, size=shape)

    # For each step, which column the current block began at...
    steps = np.arange(horizon)
    block_start_step = np.maximum.accumulate(np.where(starts_a_block, steps, -1), axis=1)
    # ...how far into the block we are, and where that block started.
    offset_in_block = steps - block_start_step
    block_origin = np.take_along_axis(candidate_starts, block_start_step, axis=1)
    return np.asarray((block_origin + offset_in_block) % source, dtype="int64")


def _draw_blocks(
    returns: np.ndarray, shape: tuple[int, int], rng: np.random.Generator, mean_block: int
) -> np.ndarray:
    """Resample runs of consecutive returns, so clustering survives."""
    drawn: np.ndarray = returns[_block_start_indices(shape, len(returns), rng, mean_block)]
    return drawn


def _draw_gbm(returns: np.ndarray, shape: tuple[int, int], rng: np.random.Generator) -> np.ndarray:
    """Normal log returns at the sample's own drift and volatility.

    A baseline rather than a realistic market: it has the right first two
    moments and none of the fat tails or clustering real returns show.
    """
    return rng.normal(float(np.mean(returns)), float(np.std(returns, ddof=1)), size=shape)


def _regime_labels(returns: np.ndarray, window: int = REGIME_WINDOW) -> np.ndarray:
    """Label each bar calm (``False``) or volatile (``True``).

    Rolling volatility split at its own median. A crude classifier by the
    standards of a fitted hidden Markov model, and deliberately so: it is
    auditable in four lines, deterministic, and needs no optimiser that
    can fail to converge. The design document records this as a proxy.
    """
    series = pd.Series(returns)
    volatility = series.rolling(window, min_periods=max(2, window // 4)).std(ddof=1)
    volatility = volatility.bfill()
    return np.asarray(volatility > volatility.median(), dtype=bool)


def _transition_matrix(labels: np.ndarray) -> np.ndarray:
    """Observed ``P(next state | current state)`` as a 2x2 matrix.

    A state never seen leaving itself would divide by zero, so an unvisited
    row falls back to "stay put", which is the least eventful assumption
    available rather than a coin flip.
    """
    matrix = np.full((2, 2), 0.5)
    for state in (0, 1):
        from_state = labels[:-1] == bool(state)
        if not from_state.any():
            matrix[state] = [1.0 - state, float(state)]
            continue
        next_states = labels[1:][from_state]
        matrix[state] = [float(np.mean(~next_states)), float(np.mean(next_states))]
    return matrix


def _regime_moments(returns: np.ndarray, labels: np.ndarray) -> np.ndarray:
    """``[[mean, sd], [mean, sd]]`` for the calm and volatile states."""
    moments = np.zeros((2, 2))
    for state in (0, 1):
        sample = returns[labels == bool(state)]
        if sample.size < 2:
            moments[state] = [float(np.mean(returns)), float(np.std(returns, ddof=1))]
            continue
        moments[state] = [float(np.mean(sample)), float(np.std(sample, ddof=1))]
    return moments


def _walk_states(shape: tuple[int, int], transitions: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Run the Markov chain forward, one column at a time.

    Sequential by nature — the whole point is that each state depends on
    the last — but only ``horizon`` iterations, each vectorised across
    paths, so the cost is in the number of bars rather than the number of
    paths.
    """
    n_paths, horizon = shape
    states = np.zeros(shape, dtype="int8")
    current = rng.integers(0, 2, size=n_paths)
    for step in range(horizon):
        states[:, step] = current
        stay_calm = rng.random(n_paths) < transitions[current, 0]
        current = np.where(stay_calm, 0, 1)
    return states


def _draw_regimes(returns: np.ndarray, shape: tuple[int, int], rng: np.random.Generator) -> np.ndarray:
    """Two-state Markov regime switching, fitted to the sample.

    Not to be confused with MCMC: the Markov chain here walks through
    *market states* to generate a path, not through parameter space to
    estimate a posterior. The design document names this trap explicitly.
    """
    labels = _regime_labels(returns)
    moments = _regime_moments(returns, labels)
    states = _walk_states(shape, _transition_matrix(labels), rng)
    means = moments[states, 0]
    deviations = moments[states, 1]
    return np.asarray(rng.normal(means, deviations), dtype="float64")


# --- the entry point ----------------------------------------------------------


def observed_log_returns(prices: pd.Series) -> np.ndarray:
    """Finite log returns of a price series, as a plain array."""
    returns = log_returns(prices.astype("float64")).to_numpy(dtype="float64")
    return returns[np.isfinite(returns)]


def _compound(start_price: float, log_return_paths: np.ndarray) -> np.ndarray:
    """Log returns to prices, with every path pinned to the same origin."""
    cumulative = np.cumsum(log_return_paths, axis=1)
    body = start_price * np.exp(cumulative)
    origin = np.full((log_return_paths.shape[0], 1), start_price)
    return np.hstack([origin, body])


def simulate_paths(
    prices: pd.Series,
    n_paths: int = 500,
    horizon: int | None = None,
    method: str = DEFAULT_GENERATOR,
    seed: int = 0,
    mean_block: int = DEFAULT_BLOCK,
) -> SimulatedPaths:
    """Generate ``n_paths`` synthetic price paths from an observed series.

    Args:
        prices: The real series the paths are modelled on.
        n_paths: How many paths to draw.
        horizon: Bars per path. Defaults to the length of the input, which
            is what makes a simulated backtest comparable with the real one.
        method: A key of :data:`GENERATORS`.
        seed: Recorded on the result; the same seed gives the same paths.
        mean_block: Mean block length for the ``block`` generator.

    Returns:
        A :class:`SimulatedPaths` carrying the paths, the method and the seed.

    Raises:
        SimulationError: unknown method, non-positive counts, or too little
            history for resampling to mean anything.
    """
    rule = generator(method)
    if n_paths < 1:
        raise SimulationError("n_paths must be at least 1")

    returns = observed_log_returns(prices)
    if returns.size < MIN_RETURNS:
        raise SimulationError(
            f"only {returns.size} usable returns; {MIN_RETURNS} are needed before resampling "
            "says anything the sample does not already say"
        )

    steps = horizon if horizon is not None else returns.size
    if steps < 1:
        raise SimulationError("horizon must be at least 1 bar")

    rng = np.random.default_rng(seed)
    shape = (n_paths, steps)
    draw: Callable[[], np.ndarray] = {
        "iid": lambda: _draw_iid(returns, shape, rng),
        "block": lambda: _draw_blocks(returns, shape, rng, mean_block),
        "gbm": lambda: _draw_gbm(returns, shape, rng),
        "regime": lambda: _draw_regimes(returns, shape, rng),
    }[rule.name]

    start_price = float(prices.dropna().iloc[0])
    return SimulatedPaths(
        prices=_compound(start_price, draw()),
        method=rule.name,
        seed=seed,
        start_price=start_price,
        source_bars=int(returns.size),
    )
