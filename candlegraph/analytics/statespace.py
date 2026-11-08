"""Model-based gap estimators: a local-level Kalman filter and a Brownian bridge.

Pure numerics, like the rest of :mod:`candlegraph.analytics`. Arrays in,
arrays out; no database, no pandas index semantics, no I/O.

``docs/roadmap/DESIGN.md`` §0 compares these with linear interpolation and
keeps both forms for different consumers:

* the **filter** (:func:`local_level_forecast`) conditions on the past only,
  so it is causal and is the one form a strategy may consume;
* the **bridge** (:func:`brownian_bridge`) conditions on the observation
  *after* the gap as well, so it is a better estimate and a look-ahead, and
  is therefore display only.

Both work in **log space**. A price is positive and multiplicative, so a
random walk in logs is the honest model: it cannot produce a negative price,
and a 1% move costs the same whether the token trades at 3 or at 30,000.

Both are deterministic. The design notes that a *sampled* path would
preserve the variance budget across a gap exactly, and that is rejected
here: a chart that changes every time you reload is not something an
operator can reason about. What they produce instead is the posterior
**mean** plus its standard deviation, so "we do not know" is representable
as a widening band rather than silently absent — which is the one thing
linear interpolation cannot do.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

# Below this, a series is effectively flat and the estimated volatility is
# noise about zero. Clamping keeps the band from collapsing to a line that
# claims more confidence than a flat window earns.
MIN_SIGMA = 1e-8
# A gap needs some history behind it before "the token's own volatility"
# means anything. With fewer returns than this the estimators still produce
# a mean, and report no band rather than one built from two observations.
MIN_RETURNS_FOR_SIGMA = 3


@dataclass(frozen=True)
class GapEstimate:
    """A filled stretch: the posterior mean, and how unsure it is.

    Both are in **log space** and have one entry per missing hour, in time
    order. ``sigma`` is one standard deviation; it is ``NaN`` when there was
    not enough history to estimate the token's volatility, which is a
    different statement from zero.
    """

    mean: np.ndarray
    sigma: np.ndarray

    def __post_init__(self) -> None:
        if self.mean.shape != self.sigma.shape:
            raise ValueError("mean and sigma must describe the same hours")

    @property
    def steps(self) -> int:
        return int(self.mean.size)

    def prices(self) -> np.ndarray:
        """The mean path back in price space."""
        return np.exp(self.mean)


def estimate_sigma(log_prices: np.ndarray) -> float:
    """Per-hour volatility of a log-price series, from its own returns.

    ``NaN`` when there is too little history to say. The caller decides what
    that means: both estimators still produce a mean, and simply report no
    band.
    """
    clean = np.asarray(log_prices, dtype="float64")
    clean = clean[np.isfinite(clean)]
    if clean.size <= MIN_RETURNS_FOR_SIGMA:
        return float("nan")
    returns = np.diff(clean)
    if returns.size < MIN_RETURNS_FOR_SIGMA:
        return float("nan")
    sigma = float(np.std(returns, ddof=1))
    if not np.isfinite(sigma):
        return float("nan")
    return max(sigma, MIN_SIGMA)


def brownian_bridge(start: float, end: float, steps: int, sigma: float) -> GapEstimate:
    """Posterior mean and s.d. of a Brownian bridge pinned at both ends.

    For a driftless random walk observed at the hours either side of a gap,
    the conditional mean at each missing hour is the straight line between
    them **in log space**, and the conditional variance is
    ``sigma^2 * k * (n + 1 - k) / (n + 1)`` — zero at the endpoints and
    widest in the middle, which is the shape an honest "we are least sure
    halfway across" has.

    Note what this says about linear interpolation: the bridge's *mean* is
    log-linear, so the two paths are close. The difference is not the line,
    it is that this one carries the band.

    Args:
        start: Log price of the last observed bar before the gap.
        end: Log price of the first observed bar after it.
        steps: How many hours are missing between them.
        sigma: Per-hour volatility, or ``NaN`` for "not estimable".

    Returns:
        A :class:`GapEstimate` over the ``steps`` missing hours.
    """
    if steps < 1:
        return GapEstimate(np.empty(0), np.empty(0))

    total = steps + 1
    k = np.arange(1, steps + 1, dtype="float64")
    mean = start + (end - start) * (k / total)
    if not np.isfinite(sigma):
        return GapEstimate(mean, np.full(steps, np.nan))
    variance = (sigma**2) * k * (total - k) / total
    return GapEstimate(mean, np.sqrt(variance))


def local_level_forecast(start: float, steps: int, sigma: float) -> GapEstimate:
    """Kalman local-level forecast across a gap: the causal form.

    The local-level (random-walk-plus-noise) model's k-step forecast is the
    current filtered level — flat — with variance growing as ``sigma^2 * k``.
    So the *mean* coincides with a forward fill, and the useful difference is
    the band: after twenty hours it is ``sqrt(20)`` times as wide as after
    one, which is the model saying it has stopped knowing.

    **A drift term was rejected.** A local-linear-trend model would
    extrapolate the last observed slope across the gap, and on hourly crypto
    a twenty-hour extrapolated trend is a confident fabrication. Flat is the
    defensible forecast for a driftless random walk, and a wrong *level* is
    recoverable in a way a wrong *direction* is not.

    Args:
        start: Filtered log level at the last observed bar before the gap.
        steps: How many hours are missing.
        sigma: Per-hour volatility estimated from data **before** the gap.

    Returns:
        A :class:`GapEstimate` over the ``steps`` missing hours.
    """
    if steps < 1:
        return GapEstimate(np.empty(0), np.empty(0))

    mean = np.full(steps, float(start))
    if not np.isfinite(sigma):
        return GapEstimate(mean, np.full(steps, np.nan))
    k = np.arange(1, steps + 1, dtype="float64")
    return GapEstimate(mean, sigma * np.sqrt(k))


def log_series(prices: pd.Series) -> pd.Series:
    """Log prices, with non-positive values dropped to ``NaN``.

    A non-positive price is not a price; letting it through would produce
    ``-inf`` and poison every window that touched it (the same rule
    :func:`analytics.rolling.log_returns` follows).
    """
    values = prices.astype("float64")
    safe = values.where(values > 0)
    return pd.Series(np.log(safe.to_numpy(dtype="float64")), index=safe.index, name=safe.name)
