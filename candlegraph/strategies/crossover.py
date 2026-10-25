"""Moving-average crossover: the trend-following counterpart to Bollinger.

Long while the fast average is above the slow one, short (optionally) while it
is below. Included mostly so the registry holds more than one philosophy —
a mean-reversion and a trend strategy fail in opposite regimes, which makes
the backtest comparison worth looking at.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from candlegraph.analytics.rolling import rolling_mean
from candlegraph.strategies.base import Signal, StrategyResult, params_of, register


class CrossoverError(ValueError):
    """The crossover configuration is inconsistent."""


@dataclass(frozen=True)
class CrossoverConfig:
    """Knobs for :func:`moving_average_crossover`.

    Attributes:
        fast: Lookback for the fast average.
        slow: Lookback for the slow average; must exceed ``fast``.
        allow_short: When False, "fast below slow" means flat, not short.
        min_separation: Minimum gap between the averages, as a fraction of
            the slow average, before a position is taken. Two averages
            crossing and re-crossing within noise would otherwise trade every
            bar.
    """

    fast: int = 12
    slow: int = 48
    allow_short: bool = False
    min_separation: float = 0.0

    def __post_init__(self) -> None:
        if self.fast < 2:
            raise CrossoverError("fast must be at least 2")
        if self.slow <= self.fast:
            raise CrossoverError(f"slow ({self.slow}) must exceed fast ({self.fast})")
        if self.min_separation < 0:
            raise CrossoverError("min_separation must not be negative")


@register(
    "crossover",
    CrossoverConfig,
    "Trend following: hold while the fast moving average leads the slow one.",
)
def moving_average_crossover(prices: pd.Series, config: CrossoverConfig | None = None) -> StrategyResult:
    """Generate trend-following positions from two moving averages.

    Args:
        prices: Close prices, oldest first, indexed by timestamp.
        config: Lookbacks and the noise filter.

    Returns:
        A :class:`StrategyResult` whose ``score`` is the separation between
        the averages as a fraction of the slow one.
    """
    config = config or CrossoverConfig()
    values = prices.astype("float64")

    fast = rolling_mean(values, config.fast)
    slow = rolling_mean(values, config.slow)
    separation = (fast - slow) / slow.where(slow != 0)

    signal = np.zeros(len(values), dtype="int64")
    gap = separation.to_numpy()
    long_side = gap > config.min_separation
    signal[long_side] = int(Signal.LONG)
    if config.allow_short:
        signal[gap < -config.min_separation] = int(Signal.SHORT)
    # Before both averages exist there is no crossover to read.
    signal[np.isnan(gap)] = int(Signal.FLAT)

    frame = pd.DataFrame(
        {
            "price": values,
            "fast": fast,
            "slow": slow,
            "score": separation,
            "signal": signal,
        }
    )

    return StrategyResult(
        name="crossover",
        symbol=str(prices.name or ""),
        frame=frame,
        params=params_of(config),
    )
