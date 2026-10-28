"""Pairwise comparison of two tokens' stored price histories.

Pure, like the rest of :mod:`candlegraph.analytics`: two aligned price series
in, numbers out. The database never appears here; the CLI loads the frame
through :mod:`candlegraph.analytics.prices` and hands the columns over.

The alignment rule is the part worth stating. Two tokens have genuinely
different trading hours — a thin token emits no row for an hour with no swaps
(invariant 19) — so every statistic here is computed **only over hours both
tokens actually have**. Correlating a real price against a forward-filled or
interpolated one manufactures correlation out of the fill policy, not the
market, so a missing hour on either side simply drops out of the comparison
and the result says how many bars survived.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from candlegraph.analytics.rolling import log_returns, periods_per_year, sharpe_ratio


@dataclass(frozen=True)
class PairwiseComparison:
    """Everything `candlegraph compare` prints, plus the rolling frame."""

    symbol_a: str
    symbol_b: str
    bars: int
    window: int
    correlation: float
    spearman: float
    beta: float
    total_return_a: float
    total_return_b: float
    relative_return: float
    volatility_a: float
    volatility_b: float
    sharpe_a: float
    sharpe_b: float
    tracking_error: float
    frame: pd.DataFrame
    """Per-bar detail: both prices, both returns, the price ratio, and the
    rolling correlation (``NaN`` until the first complete window)."""


def align_pair(a: pd.Series, b: pd.Series) -> pd.DataFrame:
    """Inner-join two price series on the hours both have.

    Returns a two-column frame named after the inputs, containing only the
    index points where **both** series have a real value. Hours only one
    token traded are excluded rather than filled — see the module docstring.
    """
    joined = pd.concat([a.astype("float64"), b.astype("float64")], axis=1, join="inner")
    return joined.dropna(how="any")


def compare_pair(
    a: pd.Series,
    b: pd.Series,
    window: int = 24,
    interval_hours: int = 1,
    risk_free_rate: float = 0.0,
) -> PairwiseComparison:
    """Compare two price series over their common history.

    Args:
        a: First token's prices, indexed by UTC timestamp, named by symbol.
        b: Second token's prices, likewise.
        window: Rolling lookback, in bars, for the rolling correlation.
        interval_hours: Width of one bar, for annualisation.
        risk_free_rate: Annual rate for the Sharpe ratios.

    Returns:
        A :class:`PairwiseComparison`. ``beta`` is ``a`` regressed on ``b``;
        ``relative_return`` is ``a``'s total return minus ``b``'s over the
        aligned bars; ``tracking_error`` is the annualised standard deviation
        of the return spread.

    Raises:
        ValueError: when fewer than three aligned bars exist — two returns is
            the minimum from which a sample correlation is defined at all.
    """
    name_a = str(a.name or "A")
    name_b = str(b.name or "B")
    aligned = align_pair(a, b)
    if len(aligned) < 3:
        raise ValueError(f"only {len(aligned)} aligned bars between {name_a} and {name_b}; need at least 3")

    prices_a = aligned.iloc[:, 0]
    prices_b = aligned.iloc[:, 1]
    returns_a = log_returns(prices_a)
    returns_b = log_returns(prices_b)
    returns = pd.concat([returns_a, returns_b], axis=1).dropna()

    variance_b = float(returns.iloc[:, 1].var(ddof=1))
    covariance = float(returns.iloc[:, 0].cov(returns.iloc[:, 1]))
    # A flat series has no variance to regress against; beta there is
    # undefined rather than infinite.
    beta = covariance / variance_b if variance_b > 0 else float("nan")

    spread = returns.iloc[:, 0] - returns.iloc[:, 1]

    frame = pd.DataFrame(
        {
            f"price_{name_a}": prices_a,
            f"price_{name_b}": prices_b,
            f"return_{name_a}": returns_a,
            f"return_{name_b}": returns_b,
            "ratio": prices_a / prices_b.where(prices_b != 0),
            "rolling_correlation": returns_a.rolling(window=window, min_periods=window).corr(returns_b),
        }
    )

    return PairwiseComparison(
        symbol_a=name_a,
        symbol_b=name_b,
        bars=len(aligned),
        window=window,
        correlation=float(returns.iloc[:, 0].corr(returns.iloc[:, 1])),
        spearman=float(returns.iloc[:, 0].corr(returns.iloc[:, 1], method="spearman")),
        beta=beta,
        total_return_a=float(prices_a.iloc[-1] / prices_a.iloc[0] - 1.0),
        total_return_b=float(prices_b.iloc[-1] / prices_b.iloc[0] - 1.0),
        relative_return=float(
            (prices_a.iloc[-1] / prices_a.iloc[0]) - (prices_b.iloc[-1] / prices_b.iloc[0])
        ),
        volatility_a=annualised_volatility(returns.iloc[:, 0], interval_hours),
        volatility_b=annualised_volatility(returns.iloc[:, 1], interval_hours),
        sharpe_a=sharpe_ratio(returns.iloc[:, 0], risk_free_rate, interval_hours),
        sharpe_b=sharpe_ratio(returns.iloc[:, 1], risk_free_rate, interval_hours),
        tracking_error=annualised_volatility(spread, interval_hours),
        frame=frame,
    )


def annualised_volatility(returns: pd.Series, interval_hours: int = 1) -> float:
    """Full-sample annualised volatility (``ddof=1``), ``0.0`` below two observations."""
    clean = returns.dropna().astype("float64")
    if len(clean) < 2:
        return 0.0
    return float(clean.std(ddof=1) * periods_per_year(interval_hours) ** 0.5)


def format_comparison(result: PairwiseComparison) -> str:
    """Render a comparison the way the CLI prints it."""
    a, b = result.symbol_a, result.symbol_b
    lines = [
        f"{a} vs {b}: {result.bars} aligned bars (hours both tokens traded)",
        f"{'correlation (pearson)':<26}{result.correlation:>12.4f}",
        f"{'correlation (spearman)':<26}{result.spearman:>12.4f}",
        f"{f'beta ({a} on {b})':<26}{result.beta:>12.4f}",
        f"{f'total return {a}':<26}{result.total_return_a:>12.2%}",
        f"{f'total return {b}':<26}{result.total_return_b:>12.2%}",
        f"{f'relative return ({a}-{b})':<26}{result.relative_return:>12.2%}",
        f"{f'volatility {a} (annual)':<26}{result.volatility_a:>12.2%}",
        f"{f'volatility {b} (annual)':<26}{result.volatility_b:>12.2%}",
        f"{f'sharpe {a}':<26}{result.sharpe_a:>12.3f}",
        f"{f'sharpe {b}':<26}{result.sharpe_b:>12.3f}",
        f"{'tracking error (annual)':<26}{result.tracking_error:>12.2%}",
    ]
    return "\n".join(lines)
