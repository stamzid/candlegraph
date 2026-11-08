"""Running a strategy over synthetic paths: is the edge luck?

A backtest reports one number over the one price path that happened —
a sample of size one. This runs the *same* strategy and the *same*
backtester over many synthetic paths with the same statistical character,
and reports the distribution.

It lives in ``strategies`` rather than ``analytics`` for one reason:
``strategies.backtest`` already imports ``analytics``, so a harness in
``analytics`` that needed the backtester would close an import cycle. The
path generators themselves are pure and stay in
:mod:`candlegraph.analytics.simulation`.

**The backtester is called, never reimplemented.** Writing
``position * returns`` here is how invariant 15 comes back, and a
look-ahead bug inside a Monte Carlo harness would be invisible — every
path would agree with every other path.

**A bootstrap of a bull sample simulates bull markets.** Resampling
widens the sample along one axis only: the ordering and recombination of
moves that actually happened. It says nothing about a regime the window
does not contain, and :data:`SAMPLE_CAVEAT` is carried on the result so
every surface repeats it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from candlegraph.analytics.simulation import (
    DEFAULT_GENERATOR,
    SimulatedPaths,
    SimulationError,
    generator,
    simulate_paths,
)
from candlegraph.strategies.backtest import BacktestConfig, BacktestResult, backtest
from candlegraph.strategies.base import run_strategy

SAMPLE_CAVEAT = (
    "Resampling reorders the moves that happened; it does not invent a regime the window "
    "does not contain. Ten thousand paths from ninety days is still ninety days."
)

# Metrics collected per path. Each is a float on BacktestResult.
TRACKED: tuple[str, ...] = ("total_return", "excess_return", "sharpe", "max_drawdown", "trades")


@dataclass(frozen=True)
class SimulationSummary:
    """What a strategy did across many synthetic markets."""

    strategy: str
    symbol: str
    method: str
    seed: int
    source_bars: int
    actual: BacktestResult
    """The real backtest, on the path that happened."""
    outcomes: pd.DataFrame
    """One row per path, one column per metric in :data:`TRACKED`."""

    @property
    def n_paths(self) -> int:
        return len(self.outcomes)

    @property
    def caveat(self) -> str:
        return SAMPLE_CAVEAT

    def p_value(self, metric: str = "excess_return") -> float:
        """Fraction of synthetic paths that did at least as well as reality.

        The empirical p-value for "this rule has an edge". Small means the
        real result is hard to reproduce on a market with this
        distribution but shuffled structure; large means it is not. A
        Sharpe of 1.2 that 40% of resampled paths also reach is not an
        edge, it is the market's shape.

        Computed with the usual ``(k + 1) / (n + 1)`` correction so the
        answer is never exactly zero: a thousand paths cannot evidence a
        p-value below about one in a thousand, and reporting ``0.000``
        would claim more than the sample size supports.
        """
        if metric not in self.outcomes.columns:
            raise SimulationError(f"no metric {metric!r}; tracked: {', '.join(TRACKED)}")
        simulated = self.outcomes[metric].to_numpy(dtype="float64")
        observed = float(getattr(self.actual, metric))
        at_least_as_good = int(np.sum(simulated >= observed))
        return (at_least_as_good + 1) / (len(simulated) + 1)

    def percentiles(
        self, metric: str = "excess_return", levels: tuple[float, ...] = (5, 25, 50, 75, 95)
    ) -> dict[str, float]:
        """Distribution of one metric across the paths."""
        values = self.outcomes[metric].to_numpy(dtype="float64")
        return {f"p{int(level)}": float(np.percentile(values, level)) for level in levels}

    @property
    def verdict(self) -> str:
        """One sentence a reader can act on, in the repo's own register."""
        p = self.p_value()
        share = f"{p:.1%}"
        if p > 0.10:
            return (
                f"No evidence of an edge: {share} of resampled paths did at least as well. "
                "That is the honest reading, not a reason to retune."
            )
        return (
            f"Hard to reproduce by chance: only {share} of resampled paths matched it. "
            "That is evidence the structure is real, not that the strategy will keep working."
        )


def _path_series(paths: SimulatedPaths, index: int, calendar: pd.Index, name: str) -> pd.Series:
    """One synthetic path as a price series on the real calendar.

    Reusing the real index keeps the simulated backtest comparable with
    the real one — same bar count, same annualisation, same costs.
    """
    values = paths.path(index)
    return pd.Series(values[: len(calendar)], index=calendar[: len(values)], name=name)


def _metrics_of(result: BacktestResult) -> dict[str, float]:
    """The tracked metrics of one backtest, as plain floats."""
    return {metric: float(getattr(result, metric)) for metric in TRACKED}


def _run_one_path(
    prices: pd.Series, strategy: str, strategy_config: Any, config: BacktestConfig
) -> dict[str, float] | None:
    """Backtest the strategy on one path, or ``None`` if it could not run.

    A single path that defeats a strategy — too few bars for a window, a
    degenerate series — is a dropped row rather than a failed run. The
    count of what survived is reported, so a mostly-empty distribution
    cannot pass for a confident one.
    """
    try:
        return _metrics_of(backtest(run_strategy(strategy, prices, strategy_config), config=config))
    except (ValueError, KeyError):
        return None


def evaluate_strategy(
    prices: pd.Series,
    strategy: str,
    n_paths: int = 500,
    method: str = DEFAULT_GENERATOR,
    seed: int = 0,
    config: BacktestConfig | None = None,
    strategy_config: Any = None,
) -> SimulationSummary:
    """Backtest a strategy on the real path and on ``n_paths`` synthetic ones.

    Args:
        prices: The observed series.
        strategy: A registered strategy name.
        n_paths: How many synthetic markets to try it on.
        method: A path generator from :mod:`analytics.simulation`.
        seed: Recorded on the result; the same seed gives the same answer.
        config: Execution assumptions, shared by the real and simulated runs.
        strategy_config: The strategy's own parameters, likewise shared.

    Returns:
        A :class:`SimulationSummary` holding the real result, the
        distribution, and the seed that produced it.

    Raises:
        SimulationError: unknown generator, or too little history.
    """
    generator(method)  # refuse an unknown name before doing any work
    settings = config or BacktestConfig()

    actual = backtest(run_strategy(strategy, prices, strategy_config), config=settings)
    paths = simulate_paths(prices, n_paths=n_paths, method=method, seed=seed)

    calendar = prices.dropna().index
    rows = [
        metrics
        for index in range(paths.n_paths)
        if (
            metrics := _run_one_path(
                _path_series(paths, index, calendar, str(prices.name or "simulated")),
                strategy,
                strategy_config,
                settings,
            )
        )
        is not None
    ]
    if not rows:
        raise SimulationError(
            f"{strategy!r} could not run on any of the {n_paths} simulated paths; "
            "the window is probably shorter than the strategy's warm-up"
        )

    return SimulationSummary(
        strategy=strategy,
        symbol=str(prices.name or ""),
        method=paths.method,
        seed=paths.seed,
        source_bars=paths.source_bars,
        actual=actual,
        outcomes=pd.DataFrame(rows, columns=list(TRACKED)),
    )


def format_simulation(summary: SimulationSummary) -> str:
    """Render a simulation the way the CLI prints it."""
    actual = summary.actual
    percentiles = summary.percentiles()
    lines = [
        f"Simulation: {summary.strategy} on {summary.symbol or '?'}",
        f"{summary.n_paths} paths, generator {summary.method!r}, seed {summary.seed}, "
        f"from {summary.source_bars} real bars",
        "-" * 58,
        f"{'actual excess vs buy & hold':<34}{actual.excess_return:>12.2%}",
        f"{'actual sharpe':<34}{actual.sharpe:>12.3f}",
        "-" * 58,
        "simulated excess, across paths:",
    ]
    lines += [f"{'  ' + name:<34}{value:>12.2%}" for name, value in percentiles.items()]
    lines += [
        "-" * 58,
        f"{'p-value (excess)':<34}{summary.p_value():>12.3f}",
        f"{'p-value (sharpe)':<34}{summary.p_value('sharpe'):>12.3f}",
        "",
        summary.verdict,
        SAMPLE_CAVEAT,
    ]
    return "\n".join(lines)
