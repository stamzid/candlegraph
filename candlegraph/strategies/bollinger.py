"""Bollinger band mean reversion.

Fade stretched prices: go long when the price is far enough below its rolling
mean, go short when it is far enough above, and flatten once it has reverted.

The part worth stating is the **hysteresis**. Entry and exit use different
thresholds (``entry_z`` > ``exit_z``), so a price hovering around a single
level does not thrash in and out once per bar. With one threshold, noise at
the boundary generates a trade per bar and fees eat the edge; the gap between
the two is what makes the position persist.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from candlegraph.analytics.rolling import bollinger_bands
from candlegraph.strategies.base import Signal, StrategyResult, params_of, register


class BollingerError(ValueError):
    """The Bollinger configuration is inconsistent."""


@dataclass(frozen=True)
class BollingerConfig:
    """Knobs for :func:`bollinger_mean_reversion`.

    Attributes:
        window: Lookback for the rolling mean and standard deviation.
        num_std: Band half-width, in sample standard deviations.
        entry_z: Absolute z-score at which a position is opened.
        exit_z: Absolute z-score at which it is closed. Must be below
            ``entry_z``; that gap is the hysteresis band.
        allow_short: When False, stretched-high prices simply mean "flat"
            rather than "short".
    """

    window: int = 20
    num_std: float = 2.0
    entry_z: float = 2.0
    exit_z: float = 0.5
    allow_short: bool = True

    def __post_init__(self) -> None:
        if self.window < 2:
            raise BollingerError("window must be at least 2")
        if self.num_std <= 0:
            raise BollingerError("num_std must be positive")
        if self.entry_z <= 0:
            raise BollingerError("entry_z must be positive")
        if self.exit_z < 0:
            raise BollingerError("exit_z must not be negative")
        if self.exit_z >= self.entry_z:
            raise BollingerError(
                f"exit_z ({self.exit_z}) must be below entry_z ({self.entry_z}); "
                "without that gap the position thrashes on noise at the threshold"
            )


def _positions(zscore: pd.Series, config: BollingerConfig) -> np.ndarray:
    """Walk the z-score, opening and closing with hysteresis.

    A loop rather than a vectorised expression because the position is
    path-dependent: whether a z of 1.0 means "hold" or "stand aside" depends
    on whether we were already in a trade.
    """
    out = np.zeros(len(zscore), dtype="int64")
    position = int(Signal.FLAT)

    for i, value in enumerate(zscore.to_numpy()):
        if np.isnan(value):
            # Before the first full window there is no opinion to hold.
            position = int(Signal.FLAT)
            out[i] = position
            continue

        if position == int(Signal.FLAT):
            if value <= -config.entry_z:
                position = int(Signal.LONG)
            elif value >= config.entry_z and config.allow_short:
                position = int(Signal.SHORT)
        elif position == int(Signal.LONG):
            # Reverted far enough, or flipped through to the other extreme.
            if value >= -config.exit_z:
                position = int(Signal.FLAT)
        elif value <= config.exit_z:
            position = int(Signal.FLAT)

        out[i] = position

    return out


@register(
    "bollinger",
    BollingerConfig,
    "Mean reversion: fade prices stretched beyond the Bollinger bands, with hysteresis.",
)
def bollinger_mean_reversion(prices: pd.Series, config: BollingerConfig | None = None) -> StrategyResult:
    """Generate mean-reversion positions from Bollinger bands.

    Args:
        prices: Close prices, oldest first, indexed by timestamp.
        config: Window, band width, and entry/exit thresholds.

    Returns:
        A :class:`StrategyResult` whose frame carries the bands, the z-score
        (as ``score``), and the target ``signal`` per bar.
    """
    config = config or BollingerConfig()
    values = prices.astype("float64")

    bands = bollinger_bands(values, window=config.window, num_std=config.num_std)
    frame = bands.copy()
    frame["price"] = values
    frame["score"] = bands["zscore"]
    frame["signal"] = _positions(bands["zscore"], config)

    return StrategyResult(
        name="bollinger",
        symbol=str(prices.name or ""),
        frame=frame,
        params=params_of(config),
    )
