"""Mean-variance optimisation: objectives, constraints, and the risk-free rate."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from candlegraph.analytics.portfolio import (
    PortfolioConfig,
    PortfolioError,
    annualised_moments,
    efficient_frontier,
    format_portfolio,
    optimise_portfolio,
    portfolio_volatility,
)
from tests.unit.analytics.conftest import hourly_index

TOLERANCE = 1e-6


def weights_sum_to_one(weights: dict[str, float]) -> bool:
    return abs(sum(weights.values()) - 1.0) < 1e-6


# --- configuration ---------------------------------------------------------


def test_an_unknown_objective_is_rejected() -> None:
    with pytest.raises(PortfolioError, match="unknown objective"):
        PortfolioConfig(objective="moon")  # type: ignore[arg-type]


def test_target_return_objective_needs_a_target() -> None:
    with pytest.raises(PortfolioError, match="needs target_return"):
        PortfolioConfig(objective="target_return")


@pytest.mark.parametrize("shrinkage", [-0.1, 1.5])
def test_shrinkage_outside_zero_to_one_is_rejected(shrinkage: float) -> None:
    with pytest.raises(PortfolioError, match="shrinkage"):
        PortfolioConfig(shrinkage=shrinkage)


def test_a_max_weight_too_small_to_reach_full_investment_is_rejected() -> None:
    """Three assets capped at 20% each can only ever sum to 0.6."""
    with pytest.raises(PortfolioError, match="cannot sum to 1"):
        PortfolioConfig(max_weight=0.2).bounds_for(3)


def test_long_only_bounds_start_at_zero() -> None:
    assert PortfolioConfig(allow_short=False).bounds_for(2)[0] == (0.0, 1.0)


def test_allowing_shorts_opens_the_lower_bound() -> None:
    low, _ = PortfolioConfig(allow_short=True, max_weight=1.0).bounds_for(2)[0]

    assert low < 0


# --- moment estimation -----------------------------------------------------


def test_moments_are_annualised(two_asset_returns: pd.DataFrame) -> None:
    mu, sigma = annualised_moments(two_asset_returns, interval_hours=1)

    raw_mean = two_asset_returns["SAFE"].mean()
    assert mu["SAFE"] == pytest.approx(raw_mean * 8760)
    # Variance scales linearly with time, so volatility scales with its root.
    raw_var = two_asset_returns["SAFE"].var(ddof=1)
    assert sigma.loc["SAFE", "SAFE"] == pytest.approx(raw_var * 8760)


def test_shrinkage_pulls_covariances_towards_the_diagonal(
    two_asset_returns: pd.DataFrame,
) -> None:
    _, plain = annualised_moments(two_asset_returns, shrinkage=0.0)
    _, shrunk = annualised_moments(two_asset_returns, shrinkage=0.5)

    assert abs(shrunk.loc["SAFE", "RISKY"]) < abs(plain.loc["SAFE", "RISKY"])
    # Variances are untouched.
    assert shrunk.loc["SAFE", "SAFE"] == pytest.approx(plain.loc["SAFE", "SAFE"])


def test_full_shrinkage_removes_every_covariance(two_asset_returns: pd.DataFrame) -> None:
    _, sigma = annualised_moments(two_asset_returns, shrinkage=1.0)

    assert sigma.loc["SAFE", "RISKY"] == pytest.approx(0.0)


def test_too_few_observations_to_estimate_is_an_error() -> None:
    frame = pd.DataFrame({"A": [0.01]}, index=hourly_index(1))

    with pytest.raises(PortfolioError, match="at least two"):
        annualised_moments(frame)


def test_ragged_rows_are_dropped_so_every_asset_shares_a_window() -> None:
    """A covariance built from different periods per asset is not a covariance."""
    frame = pd.DataFrame(
        {"A": [0.01, 0.02, np.nan, 0.01], "B": [0.01, 0.02, 0.03, 0.02]}, index=hourly_index(4)
    )

    mu, _ = annualised_moments(frame)

    assert mu["A"] == pytest.approx(np.mean([0.01, 0.02, 0.01]) * 8760)


# --- optimisation ----------------------------------------------------------


def test_weights_are_fully_invested(two_asset_returns: pd.DataFrame) -> None:
    result = optimise_portfolio(two_asset_returns)

    assert weights_sum_to_one(result.weights)
    assert result.converged


def test_long_only_produces_no_negative_weights(two_asset_returns: pd.DataFrame) -> None:
    result = optimise_portfolio(two_asset_returns, PortfolioConfig(allow_short=False))

    assert all(w >= -TOLERANCE for w in result.weights.values())


def test_minimum_volatility_tilts_towards_the_safe_asset(
    two_asset_returns: pd.DataFrame,
) -> None:
    result = optimise_portfolio(two_asset_returns, PortfolioConfig(objective="min_volatility"))

    assert result.weights["SAFE"] > result.weights["RISKY"]


def test_minimum_volatility_really_is_the_lowest_volatility(
    two_asset_returns: pd.DataFrame,
) -> None:
    minimal = optimise_portfolio(two_asset_returns, PortfolioConfig(objective="min_volatility"))
    equal = optimise_portfolio(two_asset_returns, PortfolioConfig(objective="equal_weight"))

    assert minimal.volatility <= equal.volatility + TOLERANCE


def test_max_sharpe_beats_equal_weight_on_sharpe(two_asset_returns: pd.DataFrame) -> None:
    best = optimise_portfolio(two_asset_returns, PortfolioConfig(objective="max_sharpe"))
    equal = optimise_portfolio(two_asset_returns, PortfolioConfig(objective="equal_weight"))

    assert best.sharpe >= equal.sharpe - TOLERANCE


def test_a_higher_risk_free_rate_changes_the_tangency_portfolio(
    two_asset_returns: pd.DataFrame,
) -> None:
    """The whole point of making the rate configurable."""
    cheap = optimise_portfolio(two_asset_returns, PortfolioConfig(risk_free_rate=0.0))
    dear = optimise_portfolio(two_asset_returns, PortfolioConfig(risk_free_rate=0.50))

    assert cheap.weights != dear.weights


def test_the_reported_sharpe_uses_the_configured_rate(
    two_asset_returns: pd.DataFrame,
) -> None:
    result = optimise_portfolio(two_asset_returns, PortfolioConfig(risk_free_rate=0.03))

    expected = (result.expected_return - 0.03) / result.volatility
    assert result.sharpe == pytest.approx(expected)
    assert result.risk_free_rate == 0.03


def test_equal_weight_splits_evenly(two_asset_returns: pd.DataFrame) -> None:
    result = optimise_portfolio(two_asset_returns, PortfolioConfig(objective="equal_weight"))

    assert result.weights == {"SAFE": 0.5, "RISKY": 0.5}


def test_a_weight_cap_is_respected(two_asset_returns: pd.DataFrame) -> None:
    result = optimise_portfolio(
        two_asset_returns, PortfolioConfig(objective="min_volatility", max_weight=0.6)
    )

    assert all(w <= 0.6 + TOLERANCE for w in result.weights.values())


def test_target_return_is_hit(two_asset_returns: pd.DataFrame) -> None:
    mu, _ = annualised_moments(two_asset_returns)
    target = float((mu.min() + mu.max()) / 2)

    result = optimise_portfolio(
        two_asset_returns,
        PortfolioConfig(objective="target_return", target_return=target),
    )

    assert result.converged
    assert result.expected_return == pytest.approx(target, abs=1e-4)


def test_risk_parity_equalises_risk_contributions(two_asset_returns: pd.DataFrame) -> None:
    result = optimise_portfolio(two_asset_returns, PortfolioConfig(objective="risk_parity"))
    _, sigma = annualised_moments(two_asset_returns)

    weights = np.array([result.weights[a] for a in sigma.index])
    total = portfolio_volatility(weights, sigma.to_numpy())
    contributions = weights * (sigma.to_numpy() @ weights) / total

    assert contributions[0] == pytest.approx(contributions[1], rel=0.05)
    # The safe asset must carry more notional to contribute equal risk.
    assert result.weights["SAFE"] > result.weights["RISKY"]


def test_a_single_asset_takes_the_whole_book() -> None:
    rng = np.random.default_rng(1)
    frame = pd.DataFrame({"ONLY": rng.normal(0, 0.01, 200)}, index=hourly_index(200))

    result = optimise_portfolio(frame)

    assert result.weights == {"ONLY": 1.0}
    assert result.converged


def test_an_empty_frame_is_an_error() -> None:
    with pytest.raises(PortfolioError, match="no assets"):
        optimise_portfolio(pd.DataFrame())


def test_an_all_nan_column_is_dropped(two_asset_returns: pd.DataFrame) -> None:
    frame = two_asset_returns.copy()
    frame["DEAD"] = np.nan

    result = optimise_portfolio(frame)

    assert "DEAD" not in result.weights


def test_negligible_weights_are_reported_as_zero(two_asset_returns: pd.DataFrame) -> None:
    """SLSQP returns values like 3e-17; those are noise, not positions."""
    result = optimise_portfolio(two_asset_returns, PortfolioConfig(objective="max_sharpe"))

    assert all(w == 0.0 or abs(w) >= 1e-6 for w in result.weights.values())


def test_held_lists_only_real_positions(two_asset_returns: pd.DataFrame) -> None:
    result = optimise_portfolio(two_asset_returns)

    assert set(result.held) <= set(result.weights)
    assert all(abs(w) > 0 for w in result.held.values())


def test_diversification_exceeds_one_when_assets_are_imperfectly_correlated(
    two_asset_returns: pd.DataFrame,
) -> None:
    result = optimise_portfolio(two_asset_returns, PortfolioConfig(objective="equal_weight"))

    assert result.diversification > 1.0


# --- the frontier ----------------------------------------------------------


def test_the_frontier_is_upward_sloping(two_asset_returns: pd.DataFrame) -> None:
    """More expected return must cost more risk, or it is not a frontier."""
    frontier = efficient_frontier(two_asset_returns, points=8)

    assert len(frontier) >= 3
    returns = [p.expected_return for p in frontier]
    assert returns == sorted(returns)
    assert frontier[-1].volatility >= frontier[0].volatility - TOLERANCE


def test_every_frontier_point_is_fully_invested(two_asset_returns: pd.DataFrame) -> None:
    for point in efficient_frontier(two_asset_returns, points=5):
        assert weights_sum_to_one(point.weights)


def test_a_frontier_needs_at_least_two_points(two_asset_returns: pd.DataFrame) -> None:
    with pytest.raises(PortfolioError, match="at least two points"):
        efficient_frontier(two_asset_returns, points=1)


def test_a_frontier_needs_at_least_two_assets() -> None:
    rng = np.random.default_rng(2)
    frame = pd.DataFrame({"ONLY": rng.normal(0, 0.01, 100)}, index=hourly_index(100))

    with pytest.raises(PortfolioError, match="at least two assets"):
        efficient_frontier(frame)


# --- reporting -------------------------------------------------------------


def test_the_report_names_every_asset_and_the_rate(two_asset_returns: pd.DataFrame) -> None:
    text = format_portfolio(optimise_portfolio(two_asset_returns, PortfolioConfig(risk_free_rate=0.04)))

    assert "SAFE" in text
    assert "RISKY" in text
    assert "4.00%" in text
    assert "sharpe" in text


def test_the_report_warns_when_the_solver_gave_up(two_asset_returns: pd.DataFrame) -> None:
    result = optimise_portfolio(two_asset_returns)
    stalled = type(result)(**{**result.__dict__, "converged": False, "message": "iteration limit"})

    assert "did not converge" in format_portfolio(stalled)


def test_the_frame_view_is_sorted_by_weight(two_asset_returns: pd.DataFrame) -> None:
    frame = optimise_portfolio(two_asset_returns, PortfolioConfig(objective="min_volatility")).as_frame()

    assert list(frame["weight"]) == sorted(frame["weight"], reverse=True)


# --- common sense over arithmetic -------------------------------------------------


def test_an_expected_return_below_minus_one_hundred_percent_is_capped_and_flagged() -> None:
    """Linear annualisation of a collapsing token says "-140% a year"; nothing loses more than all."""
    from tests.unit.analytics.conftest import hourly_index

    n = 500
    # A steady -0.05% per hour is about -440% a year under linear annualisation.
    collapsing = pd.DataFrame(
        {"DOOM": np.full(n, -0.0005), "ALSO": np.full(n, -0.0004) + np.linspace(0, 1e-6, n)},
        index=hourly_index(n),
    )

    result = optimise_portfolio(collapsing, PortfolioConfig(objective="equal_weight"))

    assert result.capped is True
    assert result.expected_return == -1.0
    assert "capped" in result.message
    assert "capped at -100%" in format_portfolio(result)


def test_a_sane_portfolio_is_not_flagged_as_capped(two_asset_returns: pd.DataFrame) -> None:
    result = optimise_portfolio(two_asset_returns, PortfolioConfig())

    assert result.capped is False
    assert "capped" not in format_portfolio(result)


def test_a_target_return_at_or_below_minus_one_hundred_percent_is_rejected() -> None:
    with pytest.raises(PortfolioError, match="not a return"):
        PortfolioConfig(objective="target_return", target_return=-1.0)
