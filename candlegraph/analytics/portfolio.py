"""Mean-variance portfolio construction.

Classic Markowitz, with the knobs that actually change an allocation exposed
as configuration rather than buried as constants: the risk-free rate, whether
shorting is allowed, per-asset weight bounds, the objective, and how the
covariance matrix is estimated.

Everything is annualised. Inputs are per-period returns plus the width of a
period; outputs are annual expected return, annual volatility, and a Sharpe
ratio consistent with both.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
import pandas as pd
from scipy.optimize import minimize

from candlegraph.analytics.rolling import periods_per_year

Objective = Literal["max_sharpe", "min_volatility", "target_return", "risk_parity", "equal_weight"]

OBJECTIVES: tuple[Objective, ...] = (
    "max_sharpe",
    "min_volatility",
    "target_return",
    "risk_parity",
    "equal_weight",
)

# Weights below this are reported as zero: SLSQP returns values like 3e-17
# that are noise, not positions.
WEIGHT_EPSILON = 1e-6

# The floor on a reported annual expected return. See `PortfolioResult.capped`.
MIN_EXPECTED_RETURN = -1.0


class PortfolioError(ValueError):
    """The optimisation cannot be set up or did not converge."""


@dataclass(frozen=True)
class PortfolioConfig:
    """Everything that changes the allocation.

    Attributes:
        risk_free_rate: Annual rate, e.g. ``0.04`` for 4%. Used for the Sharpe
            objective and for reporting.
        objective: Which portfolio to solve for.
        allow_short: When False (the default) weights are bounded below at 0.
        min_weight / max_weight: Per-asset bounds. ``max_weight`` below
            ``1/n`` is infeasible and is rejected up front rather than
            producing a silently unconverged result.
        target_return: Required annual return for the ``target_return``
            objective; ignored otherwise.
        interval_hours: Width of one return period, for annualisation.
        shrinkage: Ledoit-Wolf style shrinkage of the covariance matrix
            towards a diagonal, in ``[0, 1]``. Sample covariance over a short
            window is badly conditioned; a little shrinkage keeps the
            optimiser from betting the book on estimation noise.
    """

    risk_free_rate: float = 0.0
    objective: Objective = "max_sharpe"
    allow_short: bool = False
    min_weight: float | None = None
    max_weight: float = 1.0
    target_return: float | None = None
    interval_hours: int = 1
    shrinkage: float = 0.0

    def __post_init__(self) -> None:
        if self.objective not in OBJECTIVES:
            raise PortfolioError(f"unknown objective {self.objective!r}; expected one of {OBJECTIVES}")
        if not 0.0 <= self.shrinkage <= 1.0:
            raise PortfolioError("shrinkage must be between 0 and 1")
        if self.max_weight <= 0:
            raise PortfolioError("max_weight must be positive")
        if self.objective == "target_return" and self.target_return is None:
            raise PortfolioError("objective 'target_return' needs target_return to be set")
        if self.target_return is not None and self.target_return <= -1.0:
            raise PortfolioError("a target return at or below -100% is not a return a portfolio can earn")

    def bounds_for(self, n_assets: int) -> list[tuple[float, float]]:
        """Per-asset ``(low, high)`` bounds, validated for feasibility."""
        low = (
            self.min_weight
            if self.min_weight is not None
            else (-self.max_weight if self.allow_short else 0.0)
        )
        high = self.max_weight
        if low > high:
            raise PortfolioError(f"min_weight {low} exceeds max_weight {high}")
        # n assets capped at `high` each must still be able to reach 1.0.
        if high * n_assets < 1.0 - 1e-9:
            raise PortfolioError(
                f"max_weight {high} across {n_assets} assets cannot sum to 1; "
                f"raise it to at least {1 / n_assets:.3f}"
            )
        if low * n_assets > 1.0 + 1e-9:
            raise PortfolioError(f"min_weight {low} across {n_assets} assets already exceeds 1")
        return [(low, high)] * n_assets


@dataclass(frozen=True)
class PortfolioResult:
    """A solved allocation and the numbers that justify it."""

    weights: dict[str, float]
    expected_return: float
    volatility: float
    sharpe: float
    objective: str
    risk_free_rate: float
    converged: bool
    message: str = ""
    diversification: float = 0.0
    capped: bool = False
    """True when the annualised expected return fell below -100% and was
    reported as -100%. Linear annualisation of a collapsing token's mean
    log return can say "-140% a year"; nothing can lose more than everything."""

    def as_frame(self) -> pd.DataFrame:
        """Weights as a one-column frame, largest first."""
        frame = pd.DataFrame({"weight": pd.Series(self.weights, dtype="float64")})
        return frame.sort_values("weight", ascending=False)

    @property
    def held(self) -> dict[str, float]:
        """Only the assets with a non-negligible position."""
        return {k: v for k, v in self.weights.items() if abs(v) >= WEIGHT_EPSILON}


@dataclass(frozen=True)
class FrontierPoint:
    """One point on the efficient frontier."""

    expected_return: float
    volatility: float
    sharpe: float
    weights: dict[str, float] = field(default_factory=dict)


# --- estimation ------------------------------------------------------------


def annualised_moments(
    returns: pd.DataFrame, interval_hours: int = 1, shrinkage: float = 0.0
) -> tuple[pd.Series, pd.DataFrame]:
    """Annualised mean vector and covariance matrix.

    Args:
        returns: Per-period returns, one column per asset.
        interval_hours: Width of one period.
        shrinkage: Pull the covariance towards its diagonal by this fraction,
            which conditions a noisy sample estimate.

    Returns:
        ``(mu, sigma)`` — annual expected returns and annual covariance.
    """
    clean = returns.dropna(how="any").astype("float64")
    if clean.shape[0] < 2:
        raise PortfolioError("need at least two aligned observations to estimate moments")

    factor = periods_per_year(interval_hours)
    mu = clean.mean() * factor
    sigma = clean.cov(ddof=1) * factor

    if shrinkage > 0:
        diagonal = pd.DataFrame(np.diag(np.diag(sigma.to_numpy())), index=sigma.index, columns=sigma.columns)
        sigma = (1.0 - shrinkage) * sigma + shrinkage * diagonal

    return mu, sigma


def portfolio_return(weights: np.ndarray, mu: np.ndarray) -> float:
    return float(weights @ mu)


def portfolio_volatility(weights: np.ndarray, sigma: np.ndarray) -> float:
    variance = float(weights @ sigma @ weights)
    # Numerical noise can make a near-zero variance slightly negative.
    return float(np.sqrt(max(variance, 0.0)))


# --- objectives ------------------------------------------------------------


def _negative_sharpe(weights: np.ndarray, mu: np.ndarray, sigma: np.ndarray, rf: float) -> float:
    volatility = portfolio_volatility(weights, sigma)
    if volatility < 1e-12:
        # A zero-risk portfolio makes the ratio undefined; steer away from it.
        return 1e6
    return -(portfolio_return(weights, mu) - rf) / volatility


def _risk_contributions(weights: np.ndarray, sigma: np.ndarray) -> np.ndarray:
    """Each asset's share of total portfolio variance."""
    total = portfolio_volatility(weights, sigma)
    if total < 1e-12:
        return np.zeros_like(weights)
    marginal = sigma @ weights
    return np.asarray(weights * marginal / total, dtype="float64")


def _risk_parity_error(weights: np.ndarray, sigma: np.ndarray) -> float:
    """Squared spread of risk contributions; zero when every asset risks the same."""
    contributions = _risk_contributions(weights, sigma)
    return float(((contributions - contributions.mean()) ** 2).sum())


# --- the optimiser ---------------------------------------------------------


def optimise_portfolio(returns: pd.DataFrame, config: PortfolioConfig | None = None) -> PortfolioResult:
    """Solve for the configured portfolio.

    Args:
        returns: Per-period returns, one column per asset. Rows with any
            missing value are dropped so every asset is measured over exactly
            the same periods — a covariance built from ragged windows is not
            a covariance.
        config: Risk-free rate, objective, bounds. Defaults to long-only
            max-Sharpe at a zero risk-free rate.

    Returns:
        A :class:`PortfolioResult`. ``converged`` is False when the solver
        gave up, in which case the weights are its last iterate and should be
        treated as advisory.

    Raises:
        PortfolioError: no usable columns, or infeasible bounds.
    """
    config = config or PortfolioConfig()

    frame = returns.dropna(axis=1, how="all")
    if frame.empty or frame.shape[1] == 0:
        raise PortfolioError("no assets with any data to optimise over")

    mu_series, sigma_frame = annualised_moments(frame, config.interval_hours, config.shrinkage)
    assets = list(mu_series.index)
    n = len(assets)
    mu = mu_series.to_numpy()
    sigma = sigma_frame.to_numpy()

    if n == 1:
        return _single_asset_result(assets[0], mu, sigma, config)

    if config.objective == "equal_weight":
        return _result_from(np.full(n, 1.0 / n), assets, mu, sigma, config, True, "equal weight")

    bounds = config.bounds_for(n)
    constraints: list[dict[str, Any]] = [{"type": "eq", "fun": lambda w: float(w.sum() - 1.0)}]
    if config.objective == "target_return":
        target = float(config.target_return or 0.0)
        constraints.append({"type": "eq", "fun": lambda w, t=target: float(portfolio_return(w, mu) - t)})

    objective = _objective_for(config, mu, sigma)
    start = np.clip(np.full(n, 1.0 / n), bounds[0][0], bounds[0][1])

    solution = minimize(
        objective,
        start,
        method="SLSQP",
        bounds=bounds,
        constraints=constraints,
        options={"maxiter": 1000, "ftol": 1e-10},
    )

    weights = np.asarray(solution.x, dtype="float64")
    total = weights.sum()
    if abs(total) > 1e-9:
        # SLSQP satisfies the budget constraint to a tolerance; make it exact.
        weights = weights / total

    return _result_from(weights, assets, mu, sigma, config, bool(solution.success), str(solution.message))


def _objective_for(config: PortfolioConfig, mu: np.ndarray, sigma: np.ndarray) -> Any:
    match config.objective:
        case "max_sharpe":
            return lambda w: _negative_sharpe(w, mu, sigma, config.risk_free_rate)
        case "risk_parity":
            return lambda w: _risk_parity_error(w, sigma)
        case _:
            # min_volatility and target_return both minimise risk; the latter
            # just carries an extra equality constraint.
            return lambda w: portfolio_volatility(w, sigma)


def _single_asset_result(
    asset: str, mu: np.ndarray, sigma: np.ndarray, config: PortfolioConfig
) -> PortfolioResult:
    """With one asset there is nothing to optimise: it gets the whole book."""
    return _result_from(np.array([1.0]), [asset], mu, sigma, config, True, "single asset")


def _result_from(
    weights: np.ndarray,
    assets: list[str],
    mu: np.ndarray,
    sigma: np.ndarray,
    config: PortfolioConfig,
    converged: bool,
    message: str,
) -> PortfolioResult:
    cleaned = np.where(np.abs(weights) < WEIGHT_EPSILON, 0.0, weights)
    expected = portfolio_return(cleaned, mu)
    volatility = portfolio_volatility(cleaned, sigma)
    # Common sense over arithmetic: an annualised mean log return can scale
    # past -100% on a collapsing asset. A long-only book cannot lose more than
    # everything, so the report says -100% and flags that it was capped.
    capped = expected < MIN_EXPECTED_RETURN
    if capped:
        expected = MIN_EXPECTED_RETURN
        message = f"{message}; expected return capped at -100% (annualised estimate was lower)".strip("; ")
    sharpe = (expected - config.risk_free_rate) / volatility if volatility > 1e-12 else 0.0

    # Weighted average of individual vols over portfolio vol: 1.0 means no
    # diversification benefit at all.
    individual = np.sqrt(np.diag(sigma))
    weighted = float(np.abs(cleaned) @ individual)
    diversification = weighted / volatility if volatility > 1e-12 else 0.0

    return PortfolioResult(
        weights={asset: float(w) for asset, w in zip(assets, cleaned, strict=True)},
        expected_return=float(expected),
        volatility=float(volatility),
        sharpe=float(sharpe),
        objective=config.objective,
        risk_free_rate=config.risk_free_rate,
        converged=converged,
        message=message,
        diversification=float(diversification),
        capped=capped,
    )


def efficient_frontier(
    returns: pd.DataFrame, config: PortfolioConfig | None = None, points: int = 25
) -> list[FrontierPoint]:
    """Trace the frontier between the minimum-variance and maximum-return portfolios.

    Points that fail to solve are dropped rather than reported as zeros, so a
    short frontier means some targets were infeasible under the bounds.
    """
    config = config or PortfolioConfig()
    if points < 2:
        raise PortfolioError("a frontier needs at least two points")

    frame = returns.dropna(axis=1, how="all")
    mu_series, _ = annualised_moments(frame, config.interval_hours, config.shrinkage)
    if len(mu_series) < 2:
        raise PortfolioError("a frontier needs at least two assets")

    floor = optimise_portfolio(
        frame, PortfolioConfig(**{**config.__dict__, "objective": "min_volatility"})
    ).expected_return
    ceiling = float(mu_series.max())
    if ceiling <= floor:
        return []

    frontier: list[FrontierPoint] = []
    for target in np.linspace(floor, ceiling, points):
        try:
            solved = optimise_portfolio(
                frame,
                PortfolioConfig(
                    **{**config.__dict__, "objective": "target_return", "target_return": float(target)}
                ),
            )
        except PortfolioError:
            continue
        if solved.converged:
            frontier.append(
                FrontierPoint(
                    expected_return=solved.expected_return,
                    volatility=solved.volatility,
                    sharpe=solved.sharpe,
                    weights=solved.weights,
                )
            )
    return frontier


def format_portfolio(result: PortfolioResult) -> str:
    """Render an allocation as a fixed-width table for the CLI."""
    lines = [
        f"Objective: {result.objective}   risk-free: {result.risk_free_rate:.2%}",
        f"{'asset':<10}{'weight':>10}",
        "-" * 20,
    ]
    for asset, weight in sorted(result.weights.items(), key=lambda kv: -abs(kv[1])):
        lines.append(f"{asset:<10}{weight:>9.2%}")
    lines += [
        "-" * 20,
        f"{'expected':<10}{result.expected_return:>9.2%}  (annual)",
        f"{'volatility':<10}{result.volatility:>9.2%}  (annual)",
        f"{'sharpe':<10}{result.sharpe:>10.3f}",
        f"{'diversif.':<10}{result.diversification:>10.3f}",
    ]
    if not result.converged:
        lines.append(f"WARNING: optimiser did not converge: {result.message}")
    if result.capped:
        lines.append("NOTE: expected return capped at -100%; the annualised estimate was lower")
    return "\n".join(lines)
