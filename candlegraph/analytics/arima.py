"""ARIMA fitting and forecasting over a price series.

Thin, configurable wrapper over ``statsmodels``. The value added here is the
awkward parts: choosing an order by information criterion over a bounded
grid, turning a fit into a trading-usable expected return, and failing
predictably when the series is too short or the solver does not converge.

A failed fit returns a :class:`ArimaForecast` with ``ok=False`` rather than
raising, because the walk-forward signal loop must be able to skip one bad
window and carry on.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd

Criterion = Literal["aic", "bic", "hqic"]
CRITERIA: tuple[Criterion, ...] = ("aic", "bic", "hqic")

# statsmodels is noisy about non-stationarity and convergence on short
# windows. We surface both through `ok`/`message` instead.
_NOISY = (UserWarning, RuntimeWarning, FutureWarning)


class ArimaError(ValueError):
    """The ARIMA configuration itself is invalid."""


@dataclass(frozen=True)
class ArimaConfig:
    """Everything that changes an ARIMA forecast.

    Attributes:
        order: Fixed ``(p, d, q)``. Ignored when ``auto_order`` is set.
        auto_order: Search ``p <= max_p`` and ``q <= max_q`` at fixed ``d``
            and keep the best by ``criterion``.
        max_p / max_q: Grid bounds for the search.
        d: Differencing order. ``1`` is the usual choice for a price level.
        trend: statsmodels trend specification; ``"n"`` (none) is right for
            differenced prices, where a constant implies a deterministic
            drift.
        criterion: Which information criterion ranks the grid.
        use_log_prices: Fit on ``log(price)``, so differences are returns and
            the variance is closer to constant. Strongly recommended.
        min_observations: Refuse to fit on less history than this.
        forecast_steps: How many periods ahead to forecast.
        confidence: Width of the returned interval.
    """

    order: tuple[int, int, int] = (1, 1, 1)
    auto_order: bool = False
    max_p: int = 3
    max_q: int = 3
    d: int = 1
    trend: str = "n"
    criterion: Criterion = "aic"
    use_log_prices: bool = True
    min_observations: int = 60
    forecast_steps: int = 1
    confidence: float = 0.95

    def __post_init__(self) -> None:
        if len(self.order) != 3 or any(v < 0 for v in self.order):
            raise ArimaError(f"order must be three non-negative integers, got {self.order!r}")
        if self.criterion not in CRITERIA:
            raise ArimaError(f"unknown criterion {self.criterion!r}; expected one of {CRITERIA}")
        if not 0.0 < self.confidence < 1.0:
            raise ArimaError("confidence must be strictly between 0 and 1")
        if self.forecast_steps < 1:
            raise ArimaError("forecast_steps must be at least 1")
        if self.min_observations < 10:
            raise ArimaError("min_observations must be at least 10")
        if self.max_p < 0 or self.max_q < 0:
            raise ArimaError("max_p and max_q must be non-negative")


@dataclass(frozen=True)
class ArimaForecast:
    """A forecast, in price space, plus what it implies for the next period."""

    ok: bool
    order: tuple[int, int, int]
    predicted: float = float("nan")
    lower: float = float("nan")
    upper: float = float("nan")
    last_price: float = float("nan")
    criterion_value: float = float("nan")
    message: str = ""
    path: tuple[float, ...] = ()

    @property
    def expected_return(self) -> float:
        """Forecast return over the horizon, as a fraction of the last price."""
        if not self.ok or not np.isfinite(self.last_price) or self.last_price <= 0:
            return 0.0
        return float(self.predicted / self.last_price - 1.0)

    @property
    def interval_width(self) -> float:
        """Width of the confidence interval relative to the last price.

        A wide interval means the point forecast is not worth trading on, so
        the signal layer uses this to stand aside.
        """
        if not self.ok or not np.isfinite(self.last_price) or self.last_price <= 0:
            return float("inf")
        if not np.isfinite(self.upper) or not np.isfinite(self.lower):
            return float("inf")
        return float((self.upper - self.lower) / self.last_price)


def _prepare(prices: pd.Series, config: ArimaConfig) -> pd.Series:
    values = prices.dropna().astype("float64")
    if config.use_log_prices:
        positive = values.where(values > 0).dropna()
        values = pd.Series(np.log(positive.to_numpy()), index=positive.index, name=positive.name)
    return values


def _fit(series: pd.Series, order: tuple[int, int, int], trend: str) -> Any:
    from statsmodels.tsa.arima.model import ARIMA

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", _NOISY)
        # A DatetimeIndex without a frequency makes statsmodels warn and
        # refuse to label forecasts; the positional index is unambiguous and
        # we re-attach timestamps ourselves.
        model = ARIMA(series.to_numpy(), order=order, trend=trend)
        return model.fit()


def select_order(prices: pd.Series, config: ArimaConfig | None = None) -> tuple[int, int, int]:
    """Pick ``(p, d, q)`` by information criterion over the configured grid.

    Orders that fail to fit are skipped. If none fit, the configured ``order``
    is returned so the caller still has something to try.
    """
    config = config or ArimaConfig()
    series = _prepare(prices, config)
    if len(series) < config.min_observations:
        return config.order

    best_order = config.order
    best_value = float("inf")
    for p in range(config.max_p + 1):
        for q in range(config.max_q + 1):
            if p == 0 and q == 0:
                # (0,d,0) is a random walk: no parameters to rank it on.
                continue
            try:
                fitted = _fit(series, (p, config.d, q), config.trend)
                value = float(getattr(fitted, config.criterion))
            except Exception:
                continue
            if np.isfinite(value) and value < best_value:
                best_value = value
                best_order = (p, config.d, q)
    return best_order


def fit_forecast(prices: pd.Series, config: ArimaConfig | None = None) -> ArimaForecast:
    """Fit an ARIMA to ``prices`` and forecast ``forecast_steps`` ahead.

    Args:
        prices: Price levels, oldest first.
        config: Order, differencing, and horizon. Defaults to ARIMA(1,1,1)
            on log prices, one step ahead.

    Returns:
        An :class:`ArimaForecast` in **price space** — log fits are
        exponentiated back before returning, so ``predicted`` is always
        directly comparable to ``last_price``. ``ok`` is False when the
        series is too short or the fit failed; it never raises for those.
    """
    config = config or ArimaConfig()
    series = _prepare(prices, config)

    if len(series) < config.min_observations:
        return ArimaForecast(
            ok=False,
            order=config.order,
            message=f"need {config.min_observations} observations, have {len(series)}",
        )

    order = select_order(prices, config) if config.auto_order else config.order

    try:
        fitted = _fit(series, order, config.trend)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", _NOISY)
            forecast = fitted.get_forecast(steps=config.forecast_steps)
            mean = np.asarray(forecast.predicted_mean, dtype="float64")
            interval = np.asarray(forecast.conf_int(alpha=1.0 - config.confidence), dtype="float64")
    except Exception as exc:
        return ArimaForecast(ok=False, order=order, message=f"{type(exc).__name__}: {exc}")

    if mean.size == 0 or not np.isfinite(mean).all():
        return ArimaForecast(ok=False, order=order, message="forecast was not finite")

    last = float(series.iloc[-1])
    lower, upper = float(interval[-1, 0]), float(interval[-1, 1])
    path = mean.astype("float64")

    if config.use_log_prices:
        # Back out of log space. exp of the mean log-price is the median
        # forecast, which is the right point estimate to trade against.
        path, last, lower, upper = (
            np.exp(path),
            float(np.exp(last)),
            float(np.exp(lower)),
            float(np.exp(upper)),
        )

    return ArimaForecast(
        ok=True,
        order=order,
        predicted=float(path[-1]),
        lower=lower,
        upper=upper,
        last_price=last,
        criterion_value=float(getattr(fitted, config.criterion)),
        path=tuple(float(v) for v in path),
    )
