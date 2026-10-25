"""Trade signals from a walk-forward ARIMA forecast.

At each evaluation bar the model is fitted on history **up to and including
that bar only**, and asked for the next period. If the forecast return clears
a threshold, take the position.

Two things make this honest rather than a curve-fit:

* **The fit never sees the future.** Every refit slices ``prices[: i + 1]``.
  Fitting once on the whole series and reading its in-sample predictions back
  as signals is the classic way to manufacture a profitable backtest, and it
  is wrong.
* **A wide interval means no trade.** A point forecast with a confidence
  interval several percent wide carries no information worth paying fees for,
  so ``max_interval_width`` stands the strategy aside.

Refitting is expensive — it dominates the runtime of any backtest using it —
so ``refit_every`` lets the forecast be recomputed every N bars and held in
between. That is a genuine cost/fidelity trade-off, not an approximation
bug: the held signal is still only ever based on past data.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from candlegraph.analytics.arima import ArimaConfig, fit_forecast
from candlegraph.strategies.base import Signal, StrategyResult, params_of, register


class ArimaSignalError(ValueError):
    """The ARIMA signal configuration is inconsistent."""


@dataclass(frozen=True)
class ArimaSignalConfig:
    """Knobs for :func:`arima_signal`.

    Attributes:
        arima: The underlying model configuration (order, differencing,
            log prices, minimum history).
        threshold: Absolute forecast return needed to take a position, e.g.
            ``0.002`` for 20 basis points over the horizon.
        allow_short: When False, a bearish forecast means flat.
        refit_every: Refit every N bars and hold the signal in between.
            1 refits on every bar: most faithful, slowest.
        max_interval_width: Skip the trade when the confidence interval is
            wider than this fraction of the last price. ``None`` disables the
            check.
        warmup: Bars to skip before the first fit. Defaults to the model's
            ``min_observations``.
    """

    arima: ArimaConfig = field(default_factory=ArimaConfig)
    threshold: float = 0.002
    allow_short: bool = True
    refit_every: int = 6
    max_interval_width: float | None = 0.25
    warmup: int | None = None

    def __post_init__(self) -> None:
        if self.threshold < 0:
            raise ArimaSignalError("threshold must not be negative")
        if self.refit_every < 1:
            raise ArimaSignalError("refit_every must be at least 1")
        if self.max_interval_width is not None and self.max_interval_width <= 0:
            raise ArimaSignalError("max_interval_width must be positive when set")
        if self.warmup is not None and self.warmup < self.arima.min_observations:
            raise ArimaSignalError(
                f"warmup ({self.warmup}) is below the model's min_observations "
                f"({self.arima.min_observations}); the first fits would all fail"
            )

    @property
    def first_bar(self) -> int:
        return self.warmup if self.warmup is not None else self.arima.min_observations


def _position_for(expected: float, config: ArimaSignalConfig) -> int:
    if expected >= config.threshold:
        return int(Signal.LONG)
    if expected <= -config.threshold and config.allow_short:
        return int(Signal.SHORT)
    return int(Signal.FLAT)


@register(
    "arima",
    ArimaSignalConfig,
    "Walk-forward ARIMA: trade the sign of the next-period forecast return.",
)
def arima_signal(prices: pd.Series, config: ArimaSignalConfig | None = None) -> StrategyResult:
    """Generate positions from a walk-forward ARIMA forecast.

    Args:
        prices: Close prices, oldest first, indexed by timestamp.
        config: Model, threshold, and refit cadence.

    Returns:
        A :class:`StrategyResult` whose ``score`` is the forecast return and
        whose frame also carries ``forecast``, ``interval_width`` and
        ``refit`` (True on bars where the model was actually fitted).
    """
    config = config or ArimaSignalConfig()
    values = prices.astype("float64")
    n = len(values)

    signal = np.zeros(n, dtype="int64")
    score = np.full(n, np.nan)
    forecast = np.full(n, np.nan)
    width = np.full(n, np.nan)
    refit = np.zeros(n, dtype=bool)

    position = int(Signal.FLAT)
    last_expected = np.nan
    last_forecast = np.nan
    last_width = np.nan

    for i in range(config.first_bar, n):
        bars_since_start = i - config.first_bar
        if bars_since_start % config.refit_every == 0:
            # `: i + 1` — history through the current bar's close, nothing after.
            result = fit_forecast(values.iloc[: i + 1], config.arima)
            refit[i] = True

            if not result.ok:
                position = int(Signal.FLAT)
                last_expected = last_forecast = last_width = np.nan
            else:
                last_expected = result.expected_return
                last_forecast = result.predicted
                last_width = result.interval_width
                too_uncertain = (
                    config.max_interval_width is not None and last_width > config.max_interval_width
                )
                position = int(Signal.FLAT) if too_uncertain else _position_for(last_expected, config)

        signal[i] = position
        score[i] = last_expected
        forecast[i] = last_forecast
        width[i] = last_width

    frame = pd.DataFrame(
        {
            "price": values,
            "forecast": forecast,
            "interval_width": width,
            "score": score,
            "signal": signal,
            "refit": refit,
        },
        index=values.index,
    )

    return StrategyResult(
        name="arima",
        symbol=str(prices.name or ""),
        frame=frame,
        params=params_of(config),
    )
