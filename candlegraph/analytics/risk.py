"""Tail risk and risk decomposition over a return series.

:mod:`candlegraph.analytics.rolling` answers "how much does this move" with a
standard deviation and "how far did it fall" with a drawdown. This module
answers two questions those leave open:

* **How bad is a bad period?** Value at Risk (VaR) is the loss that is only
  exceeded ``1 - confidence`` of the time; Conditional VaR (CVaR, expected
  shortfall) is the average loss *given* that it is exceeded. Three
  estimators are offered because they disagree exactly when it matters —
  in fat tails — and a reader should be able to see that disagreement.
* **Which part of the risk is the market, and which part is the token?**
  Regressing an asset's returns on a benchmark's splits its variance into a
  systematic share (``beta²·Var(benchmark)``) that diversification cannot
  remove and an idiosyncratic remainder that it can.

Conventions, stated once:

* **Losses are positive numbers.** A VaR of ``0.03`` means "a 3% loss or
  worse". Returns are the arithmetic kind, because a loss is a fraction of
  money, not a log; at hourly horizons the difference from log returns is
  within rounding.
* **Multi-bar horizons scale by the square root of time.** That is exact for
  the parametric estimator under its own assumptions and an approximation
  for the historical one; the report says which horizon it used so the
  reader can judge.
* **Pure.** Nothing here touches the database (invariant 18); the CLI loads
  prices and hands them in.

The theory is written up in ``docs/analytics/risk/DESIGN.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd
from scipy import stats

from candlegraph.analytics.rolling import (
    drawdown_curve,
    equity_curve,
    periods_per_year,
    sharpe_ratio,
    simple_returns,
    sortino_ratio,
)

Method = Literal["historical", "parametric", "cornish_fisher"]
METHODS: tuple[Method, ...] = ("historical", "parametric", "cornish_fisher")


class RiskError(ValueError):
    """The risk configuration is invalid or the series is too short."""


@dataclass(frozen=True)
class RiskConfig:
    """Every knob that changes a tail-risk number.

    Attributes:
        confidence: The VaR confidence level, e.g. ``0.95``: the loss that is
            exceeded only 5% of the time. Must be above one half, because a
            "VaR" at 40% confidence would describe a typical bar, not a tail.
        horizon_bars: How many bars the loss is measured over. ``24`` on
            hourly data is a one-day VaR.
        method: ``historical`` reads the empirical quantile; ``parametric``
            assumes normal returns; ``cornish_fisher`` adjusts the normal
            quantile for the sample's skew and kurtosis.
        interval_hours: Width of one bar, for annualisation.
        risk_free_rate: Annual rate for the Sharpe and Sortino ratios in the
            report.
        min_observations: Refuse to estimate a tail from fewer bars than
            this. A 95% VaR from 20 bars is the single worst bar, not a
            statistic.
    """

    confidence: float = 0.95
    horizon_bars: int = 1
    method: Method = "historical"
    interval_hours: int = 1
    risk_free_rate: float = 0.0
    min_observations: int = 30

    def __post_init__(self) -> None:
        if not 0.5 < self.confidence < 1.0:
            raise RiskError("confidence must be strictly between 0.5 and 1")
        if self.horizon_bars < 1:
            raise RiskError("horizon_bars must be at least 1")
        if self.method not in METHODS:
            raise RiskError(f"unknown method {self.method!r}; expected one of {METHODS}")
        if self.interval_hours < 1:
            raise RiskError("interval_hours must be at least 1")
        if self.min_observations < 10:
            raise RiskError("min_observations must be at least 10")

    @property
    def tail(self) -> float:
        """The tail probability, ``1 - confidence``."""
        return 1.0 - self.confidence


def _clean(returns: pd.Series, config: RiskConfig) -> np.ndarray:
    values = returns.dropna().astype("float64").to_numpy()
    values = values[np.isfinite(values)]
    if len(values) < config.min_observations:
        raise RiskError(
            f"need at least {config.min_observations} returns to estimate a tail, have {len(values)}"
        )
    return values


def _cornish_fisher_z(z: float, skew: float, excess_kurtosis: float) -> float:
    """The normal quantile ``z`` bent by the sample's third and fourth moments.

    With zero skew and zero excess kurtosis this is exactly ``z``, so the
    estimator collapses to the parametric one on normal data.
    """
    s, k = skew, excess_kurtosis
    return float(
        z + (z**2 - 1.0) * s / 6.0 + (z**3 - 3.0 * z) * k / 24.0 - (2.0 * z**3 - 5.0 * z) * s**2 / 36.0
    )


def value_at_risk(returns: pd.Series, config: RiskConfig | None = None) -> float:
    """Value at Risk over ``horizon_bars``, as a positive loss fraction.

    Args:
        returns: Per-bar arithmetic returns.
        config: Confidence, horizon and estimator.

    Returns:
        The loss exceeded with probability ``1 - confidence``. Can be
        negative when the mean return is large relative to the dispersion —
        that is the honest answer, not an error.

    Raises:
        RiskError: too few observations.
    """
    config = config or RiskConfig()
    values = _clean(returns, config)
    scale = float(np.sqrt(config.horizon_bars))
    mu = float(values.mean())
    sigma = float(values.std(ddof=1))

    match config.method:
        case "historical":
            # The empirical tail. Scaled like the parametric one so horizons
            # are comparable across methods; an approximation, and said so.
            quantile = float(np.quantile(values, config.tail))
            return -quantile * scale
        case "parametric":
            z = float(stats.norm.ppf(config.tail))
            return -(mu * config.horizon_bars + z * sigma * scale)
        case _:
            z = float(stats.norm.ppf(config.tail))
            z_cf = _cornish_fisher_z(z, float(stats.skew(values)), float(stats.kurtosis(values)))
            return -(mu * config.horizon_bars + z_cf * sigma * scale)


def conditional_value_at_risk(returns: pd.Series, config: RiskConfig | None = None) -> float:
    """Expected shortfall: the mean loss on the bars that breach the VaR.

    Always at least the VaR for the same configuration — the average of the
    tail cannot be better than its edge.

    Raises:
        RiskError: too few observations.
    """
    config = config or RiskConfig()
    values = _clean(returns, config)
    scale = float(np.sqrt(config.horizon_bars))
    mu = float(values.mean())
    sigma = float(values.std(ddof=1))
    tail = config.tail

    match config.method:
        case "historical":
            quantile = float(np.quantile(values, tail))
            breaches = values[values <= quantile]
            return -float(breaches.mean()) * scale
        case "parametric":
            # Closed form for a normal: the density at the quantile over the
            # tail mass, in standard deviations.
            z = float(stats.norm.ppf(tail))
            shortfall_z = float(stats.norm.pdf(z)) / tail
            return shortfall_z * sigma * scale - mu * config.horizon_bars
        case _:
            # No closed form: average the Cornish-Fisher quantile over the
            # tail probabilities below `tail`.
            skew, kurt = float(stats.skew(values)), float(stats.kurtosis(values))
            grid = np.linspace(tail / 400.0, tail, 400)
            z_grid = np.array([_cornish_fisher_z(float(stats.norm.ppf(p)), skew, kurt) for p in grid])
            return -(mu * config.horizon_bars + float(z_grid.mean()) * sigma * scale)


# --- systematic versus idiosyncratic ---------------------------------------


@dataclass(frozen=True)
class RiskDecomposition:
    """An asset's variance split into the part a benchmark explains and the rest.

    Attributes:
        benchmark: The benchmark's name.
        bars: Aligned bars the regression used.
        beta: Slope of the asset's returns on the benchmark's.
        alpha: Annualised intercept — the return not explained by beta.
        r_squared: Share of the asset's variance the benchmark explains;
            equal to the squared correlation.
        total_volatility: Annualised volatility of the asset.
        systematic_volatility: ``|beta| · benchmark volatility``, annualised.
        idiosyncratic_volatility: What is left, annualised.
        systematic_share: ``systematic variance / total variance``; the
            fraction diversification cannot remove.
    """

    benchmark: str
    bars: int
    beta: float
    alpha: float
    r_squared: float
    total_volatility: float
    systematic_volatility: float
    idiosyncratic_volatility: float
    systematic_share: float

    @property
    def idiosyncratic_share(self) -> float:
        return 1.0 - self.systematic_share


def decompose_risk(
    asset_returns: pd.Series, benchmark_returns: pd.Series, interval_hours: int = 1
) -> RiskDecomposition:
    """Split an asset's return variance into systematic and idiosyncratic parts.

    Both series are inner-joined first, so only bars both have are used —
    the same rule as :mod:`candlegraph.analytics.pairwise`, for the same
    reason: a return regressed against a filled one measures the fill.

    The split is exact by construction: ``systematic = beta² · Var(benchmark)``
    and ``idiosyncratic = total - systematic``, which the Cauchy-Schwarz
    inequality keeps non-negative. The two shares therefore sum to one.

    Raises:
        RiskError: fewer than three aligned bars, or a benchmark with no
            variance to regress against.
    """
    joined = pd.concat(
        [asset_returns.astype("float64"), benchmark_returns.astype("float64")], axis=1, join="inner"
    ).dropna(how="any")
    if len(joined) < 3:
        raise RiskError(f"only {len(joined)} aligned bars; need at least 3 to regress")

    a = joined.iloc[:, 0].to_numpy()
    b = joined.iloc[:, 1].to_numpy()
    var_a = float(np.var(a, ddof=1))
    var_b = float(np.var(b, ddof=1))
    if var_b <= 0:
        raise RiskError("the benchmark has no variance; beta is undefined")

    covariance = float(np.cov(a, b, ddof=1)[0, 1])
    beta = covariance / var_b
    factor = periods_per_year(interval_hours)
    alpha = (float(a.mean()) - beta * float(b.mean())) * factor

    systematic_var = beta**2 * var_b
    idiosyncratic_var = max(var_a - systematic_var, 0.0)
    share = systematic_var / var_a if var_a > 0 else 0.0

    return RiskDecomposition(
        benchmark=str(benchmark_returns.name or "benchmark"),
        bars=len(joined),
        beta=beta,
        alpha=alpha,
        r_squared=share,
        total_volatility=float(np.sqrt(var_a * factor)),
        systematic_volatility=float(np.sqrt(systematic_var * factor)),
        idiosyncratic_volatility=float(np.sqrt(idiosyncratic_var * factor)),
        systematic_share=share,
    )


# --- the report --------------------------------------------------------------


@dataclass(frozen=True)
class RiskReport:
    """Everything ``candlegraph risk`` prints."""

    symbol: str
    bars: int
    config: RiskConfig
    annualised_return: float
    annualised_volatility: float
    downside_deviation: float
    max_drawdown: float
    var: float
    cvar: float
    sharpe: float
    sortino: float
    calmar: float
    skew: float
    excess_kurtosis: float
    decomposition: RiskDecomposition | None = None

    @property
    def horizon_label(self) -> str:
        hours = self.config.horizon_bars * self.config.interval_hours
        return f"{hours}h" if hours % 24 else f"{hours // 24}d"


def downside_deviation(returns: pd.Series, interval_hours: int = 1) -> float:
    """Annualised root-mean-square of the negative returns only."""
    clean = returns.dropna().astype("float64")
    losses = clean[clean < 0]
    if len(losses) < 2:
        return 0.0
    return float(np.sqrt((losses**2).mean()) * np.sqrt(periods_per_year(interval_hours)))


def risk_report(
    prices: pd.Series,
    config: RiskConfig | None = None,
    benchmark_prices: pd.Series | None = None,
) -> RiskReport:
    """Build the full risk picture for one price series.

    Args:
        prices: Price levels, oldest first, named by symbol.
        config: Confidence, horizon, estimator, and the rate for the ratios.
        benchmark_prices: When given, the systematic/idiosyncratic split is
            computed against it over aligned bars.

    Raises:
        RiskError: too little history for the configured tail.
    """
    config = config or RiskConfig()
    values = prices.astype("float64").dropna()
    returns = simple_returns(values).dropna()
    clean = _clean(returns, config)

    factor = periods_per_year(config.interval_hours)
    bars = len(clean)
    growth = float(values.iloc[-1] / values.iloc[0]) if values.iloc[0] > 0 else 0.0
    annualised = float(growth ** (factor / bars) - 1.0) if growth > 0 else -1.0
    drawdown = float(drawdown_curve(equity_curve(returns)).min())

    decomposition = None
    if benchmark_prices is not None:
        decomposition = decompose_risk(
            returns, simple_returns(benchmark_prices.astype("float64")), config.interval_hours
        )

    return RiskReport(
        symbol=str(prices.name or ""),
        bars=bars,
        config=config,
        annualised_return=annualised,
        annualised_volatility=float(clean.std(ddof=1) * np.sqrt(factor)),
        downside_deviation=downside_deviation(returns, config.interval_hours),
        max_drawdown=drawdown,
        var=value_at_risk(returns, config),
        cvar=conditional_value_at_risk(returns, config),
        sharpe=sharpe_ratio(returns, config.risk_free_rate, config.interval_hours),
        sortino=sortino_ratio(returns, config.risk_free_rate, config.interval_hours),
        calmar=annualised / abs(drawdown) if drawdown < 0 else 0.0,
        skew=float(stats.skew(clean)),
        excess_kurtosis=float(stats.kurtosis(clean)),
        decomposition=decomposition,
    )


def portfolio_returns(returns: pd.DataFrame, weights: dict[str, float]) -> pd.Series:
    """The per-bar return of a fixed-weight portfolio, over bars every asset has.

    Weights for assets missing from the frame are ignored; assets in the
    frame with no weight contribute nothing. Rows with any missing return are
    dropped so the portfolio is marked over one common window.
    """
    columns = [name for name in returns.columns if name in weights]
    if not columns:
        raise RiskError("none of the weighted assets are in the returns frame")
    frame = returns[columns].dropna(how="any").astype("float64")
    vector = np.array([weights[name] for name in columns], dtype="float64")
    series: pd.Series = frame.to_numpy() @ vector
    return pd.Series(series, index=frame.index, name="portfolio")


def format_risk_report(report: RiskReport) -> str:
    """Render a risk report as a fixed-width table for the CLI."""
    config = report.config
    lines = [
        f"Risk: {report.symbol} over {report.bars} bars "
        f"({config.method}, {config.confidence:.0%} confidence, {report.horizon_label} horizon)",
        "-" * 44,
        f"{'annualised return':<26}{report.annualised_return:>14.2%}",
        f"{'volatility (annual)':<26}{report.annualised_volatility:>14.2%}",
        f"{'downside dev. (annual)':<26}{report.downside_deviation:>14.2%}",
        f"{'max drawdown':<26}{report.max_drawdown:>14.2%}",
        f"{f'VaR {config.confidence:.0%} {report.horizon_label}':<26}{report.var:>14.2%}",
        f"{f'CVaR {config.confidence:.0%} {report.horizon_label}':<26}{report.cvar:>14.2%}",
        f"{'sharpe':<26}{report.sharpe:>14.3f}",
        f"{'sortino':<26}{report.sortino:>14.3f}",
        f"{'calmar':<26}{report.calmar:>14.3f}",
        f"{'skew':<26}{report.skew:>14.3f}",
        f"{'excess kurtosis':<26}{report.excess_kurtosis:>14.3f}",
    ]
    if report.decomposition is not None:
        d = report.decomposition
        lines += [
            "-" * 44,
            f"versus {d.benchmark} over {d.bars} aligned bars",
            f"{'beta':<26}{d.beta:>14.3f}",
            f"{'alpha (annual)':<26}{d.alpha:>14.2%}",
            f"{'r-squared':<26}{d.r_squared:>14.3f}",
            f"{'systematic vol.':<26}{d.systematic_volatility:>14.2%}",
            f"{'idiosyncratic vol.':<26}{d.idiosyncratic_volatility:>14.2%}",
            f"{'systematic share':<26}{d.systematic_share:>14.1%}",
        ]
    return "\n".join(lines)
