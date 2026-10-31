"""Tail risk and the systematic/idiosyncratic split, on series with known answers.

Where a closed form exists (normal data, a benchmark that is the asset itself)
the tests check it; elsewhere they check the orderings that must hold for any
data: CVaR is never better than VaR, shares sum to one, more confidence means
a bigger loss.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from scipy import stats

from candlegraph.analytics.risk import (
    RiskConfig,
    RiskError,
    conditional_value_at_risk,
    decompose_risk,
    downside_deviation,
    format_risk_report,
    portfolio_returns,
    risk_report,
    value_at_risk,
)
from candlegraph.analytics.rolling import periods_per_year, simple_returns
from tests.unit.analytics.conftest import SEED, hourly_index, random_walk

N = 5000


def normal_returns(mu: float = 0.0, sigma: float = 0.01, n: int = N, seed: int = SEED) -> pd.Series:
    rng = np.random.default_rng(seed)
    return pd.Series(rng.normal(mu, sigma, n), index=hourly_index(n), name="NORMAL")


def fat_tailed_returns(n: int = N, seed: int = SEED) -> pd.Series:
    """Student-t with 3 degrees of freedom: the tails are far heavier than normal."""
    rng = np.random.default_rng(seed)
    return pd.Series(0.01 * rng.standard_t(3, n), index=hourly_index(n), name="FAT")


# --- configuration -----------------------------------------------------------


@pytest.mark.parametrize("confidence", [0.0, 0.5, 1.0, 1.2])
def test_confidence_must_describe_a_tail(confidence: float) -> None:
    with pytest.raises(RiskError, match="confidence"):
        RiskConfig(confidence=confidence)


def test_an_unknown_method_is_rejected() -> None:
    with pytest.raises(RiskError, match="method"):
        RiskConfig(method="magic")  # type: ignore[arg-type]


def test_the_horizon_must_be_at_least_one_bar() -> None:
    with pytest.raises(RiskError, match="horizon"):
        RiskConfig(horizon_bars=0)


def test_too_few_observations_is_an_error_not_a_guess() -> None:
    """A 95% VaR from 20 bars is the single worst bar wearing a statistic's name."""
    with pytest.raises(RiskError, match="need at least"):
        value_at_risk(normal_returns(n=20))


# --- value at risk -------------------------------------------------------------


def test_historical_var_is_the_empirical_tail_quantile() -> None:
    returns = normal_returns()

    var = value_at_risk(returns, RiskConfig(confidence=0.95, method="historical"))

    assert var == pytest.approx(-float(np.quantile(returns, 0.05)))
    assert var > 0


def test_parametric_var_matches_the_normal_closed_form() -> None:
    returns = normal_returns(mu=0.0001, sigma=0.01)
    mu, sigma = float(returns.mean()), float(returns.std(ddof=1))

    var = value_at_risk(returns, RiskConfig(confidence=0.99, method="parametric"))

    assert var == pytest.approx(-(mu + stats.norm.ppf(0.01) * sigma))


def test_cornish_fisher_collapses_to_parametric_on_symmetric_thin_tailed_data() -> None:
    """With skew and excess kurtosis near zero the correction is near zero."""
    returns = normal_returns(n=20000)
    parametric = value_at_risk(returns, RiskConfig(method="parametric", confidence=0.95))
    adjusted = value_at_risk(returns, RiskConfig(method="cornish_fisher", confidence=0.95))

    assert adjusted == pytest.approx(parametric, rel=0.03)


def test_fat_tails_make_the_normal_assumption_understate_the_loss() -> None:
    """The reason three estimators exist: on t(3) data the parametric VaR is too small."""
    returns = fat_tailed_returns()
    historical = value_at_risk(returns, RiskConfig(method="historical", confidence=0.99))
    parametric = value_at_risk(returns, RiskConfig(method="parametric", confidence=0.99))
    adjusted = value_at_risk(returns, RiskConfig(method="cornish_fisher", confidence=0.99))

    assert parametric < historical, "a normal fit cannot see a fat tail"
    assert adjusted > parametric, "the kurtosis correction pushes the quantile outward"


def test_more_confidence_means_a_larger_loss() -> None:
    returns = normal_returns()

    assert value_at_risk(returns, RiskConfig(confidence=0.99)) > value_at_risk(
        returns, RiskConfig(confidence=0.95)
    )


def test_the_horizon_scales_parametric_var_by_the_square_root_of_time() -> None:
    """A zero-mean series: a 4-bar VaR is exactly twice the 1-bar VaR."""
    returns = normal_returns()
    centred = returns - returns.mean()

    one = value_at_risk(centred, RiskConfig(method="parametric", horizon_bars=1))
    four = value_at_risk(centred, RiskConfig(method="parametric", horizon_bars=4))

    assert four == pytest.approx(2.0 * one)


# --- conditional value at risk ---------------------------------------------------


@pytest.mark.parametrize("method", ["historical", "parametric", "cornish_fisher"])
def test_cvar_is_never_better_than_var(method: str) -> None:
    """The average of the tail cannot be less than its edge, whatever the estimator."""
    for returns in (normal_returns(), fat_tailed_returns()):
        config = RiskConfig(confidence=0.95, method=method)  # type: ignore[arg-type]
        assert conditional_value_at_risk(returns, config) >= value_at_risk(returns, config)


def test_historical_cvar_is_the_mean_of_the_breaching_bars() -> None:
    returns = normal_returns()
    cutoff = float(np.quantile(returns, 0.05))

    cvar = conditional_value_at_risk(returns, RiskConfig(confidence=0.95))

    assert cvar == pytest.approx(-float(returns[returns <= cutoff].mean()))


def test_parametric_cvar_matches_the_normal_closed_form() -> None:
    returns = normal_returns()
    mu, sigma = float(returns.mean()), float(returns.std(ddof=1))
    z = stats.norm.ppf(0.05)

    cvar = conditional_value_at_risk(returns, RiskConfig(confidence=0.95, method="parametric"))

    assert cvar == pytest.approx(sigma * stats.norm.pdf(z) / 0.05 - mu)


# --- systematic versus idiosyncratic ---------------------------------------------


def market_and_asset(beta: float = 1.5, noise: float = 0.005, n: int = N) -> tuple[pd.Series, pd.Series]:
    rng = np.random.default_rng(SEED)
    market = pd.Series(rng.normal(0.0, 0.01, n), index=hourly_index(n), name="MKT")
    asset = pd.Series(beta * market + rng.normal(0.0, noise, n), index=hourly_index(n), name="ASSET")
    return asset, market


def test_the_shares_sum_to_one_and_the_split_is_exact() -> None:
    asset, market = market_and_asset()

    split = decompose_risk(asset, market)

    assert split.systematic_share + split.idiosyncratic_share == pytest.approx(1.0)
    assert split.systematic_volatility**2 + split.idiosyncratic_volatility**2 == pytest.approx(
        split.total_volatility**2
    )


def test_beta_and_the_systematic_share_recover_the_generating_model() -> None:
    asset, market = market_and_asset(beta=1.5, noise=0.005)

    split = decompose_risk(asset, market)

    # Var(asset) = 1.5²·0.01² + 0.005² = 0.000250; systematic = 0.000225 → 90%.
    assert split.beta == pytest.approx(1.5, rel=0.05)
    assert split.systematic_share == pytest.approx(0.9, abs=0.02)
    assert split.r_squared == pytest.approx(split.systematic_share)


def test_r_squared_is_the_squared_correlation() -> None:
    asset, market = market_and_asset()

    split = decompose_risk(asset, market)

    assert split.r_squared == pytest.approx(float(asset.corr(market)) ** 2)


def test_an_asset_against_itself_is_all_systematic() -> None:
    returns = normal_returns()

    split = decompose_risk(returns, returns.rename("SELF"))

    assert split.beta == pytest.approx(1.0)
    assert split.systematic_share == pytest.approx(1.0)
    assert split.idiosyncratic_volatility == pytest.approx(0.0, abs=1e-9)


def test_an_independent_asset_is_nearly_all_idiosyncratic() -> None:
    asset = normal_returns(seed=1)
    market = normal_returns(seed=2).rename("MKT")

    split = decompose_risk(asset, market)

    assert abs(split.beta) < 0.05
    assert split.systematic_share < 0.01


def test_the_decomposition_uses_only_aligned_bars() -> None:
    """A thin asset against a liquid benchmark: the hours it did not trade drop out."""
    asset, market = market_and_asset()
    sparse = asset.iloc[::3]

    split = decompose_risk(sparse, market)

    assert split.bars == len(sparse)


def test_a_flat_benchmark_has_no_beta() -> None:
    asset = normal_returns()
    flat = pd.Series(np.zeros(N), index=asset.index, name="FLAT")

    with pytest.raises(RiskError, match="no variance"):
        decompose_risk(asset, flat)


def test_too_few_aligned_bars_is_an_error() -> None:
    a = pd.Series([0.01, 0.02], index=hourly_index(2))
    b = pd.Series([0.01, -0.02], index=hourly_index(2))

    with pytest.raises(RiskError, match="aligned bars"):
        decompose_risk(a, b)


def test_volatilities_are_annualised_from_the_bar_width() -> None:
    asset, market = market_and_asset()
    hourly = decompose_risk(asset, market, interval_hours=1)
    daily = decompose_risk(asset, market, interval_hours=24)

    # Same per-bar variance, fewer bars per year: a smaller annual figure.
    assert hourly.total_volatility / daily.total_volatility == pytest.approx(
        np.sqrt(periods_per_year(1) / periods_per_year(24))
    )


# --- the report ------------------------------------------------------------------


def test_the_report_carries_every_headline_number(walk: pd.Series) -> None:
    report = risk_report(walk, RiskConfig(confidence=0.95, horizon_bars=24))

    assert report.symbol == "WALK"
    assert report.bars == len(walk) - 1
    assert report.cvar >= report.var
    assert report.max_drawdown <= 0
    assert report.annualised_volatility > 0
    assert report.decomposition is None
    assert report.horizon_label == "1d"


def test_the_report_splits_risk_when_a_benchmark_is_given() -> None:
    benchmark = random_walk(seed=1).rename("BENCH")
    asset = random_walk(seed=2).rename("ASSET")

    report = risk_report(asset, benchmark_prices=benchmark)

    assert report.decomposition is not None
    assert report.decomposition.benchmark == "BENCH"
    assert 0.0 <= report.decomposition.systematic_share <= 1.0


def test_downside_deviation_ignores_the_gains() -> None:
    gains_only = pd.Series(np.full(100, 0.01), index=hourly_index(100))

    assert downside_deviation(gains_only) == 0.0


def test_the_formatted_report_names_the_method_and_the_horizon(walk: pd.Series) -> None:
    text = format_risk_report(
        risk_report(walk, RiskConfig(method="parametric", horizon_bars=24), random_walk(seed=3).rename("B"))
    )

    assert "parametric" in text
    assert "VaR 95% 1d" in text
    assert "CVaR 95% 1d" in text
    assert "versus B" in text
    assert "systematic share" in text


# --- portfolio returns -----------------------------------------------------------


def test_portfolio_returns_are_the_weighted_sum_over_common_bars(two_asset_returns: pd.DataFrame) -> None:
    weights = {"SAFE": 0.25, "RISKY": 0.75}

    series = portfolio_returns(two_asset_returns, weights)

    expected = 0.25 * two_asset_returns["SAFE"] + 0.75 * two_asset_returns["RISKY"]
    assert np.allclose(series.to_numpy(), expected.to_numpy())


def test_portfolio_returns_drop_bars_any_asset_is_missing(two_asset_returns: pd.DataFrame) -> None:
    ragged = two_asset_returns.copy()
    ragged.iloc[5, 0] = np.nan

    series = portfolio_returns(ragged, {"SAFE": 0.5, "RISKY": 0.5})

    assert len(series) == len(ragged) - 1


def test_portfolio_returns_need_at_least_one_weighted_asset(two_asset_returns: pd.DataFrame) -> None:
    with pytest.raises(RiskError, match="none of the weighted assets"):
        portfolio_returns(two_asset_returns, {"GOLD": 1.0})


def test_a_diversified_portfolio_has_a_smaller_var_than_its_riskiest_leg(
    two_asset_returns: pd.DataFrame,
) -> None:
    config = RiskConfig(confidence=0.95)

    mixed = value_at_risk(portfolio_returns(two_asset_returns, {"SAFE": 0.5, "RISKY": 0.5}), config)
    risky = value_at_risk(two_asset_returns["RISKY"], config)

    assert mixed < risky


def test_var_on_prices_uses_simple_returns(walk: pd.Series) -> None:
    """A loss is a fraction of money, so the report is built on arithmetic returns."""
    report = risk_report(walk)

    assert report.var == pytest.approx(value_at_risk(simple_returns(walk).dropna()))


# --- a loss cannot exceed everything ---------------------------------------------


@pytest.mark.parametrize("method", ["historical", "parametric", "cornish_fisher"])
def test_no_estimator_reports_a_loss_beyond_one_hundred_percent(method: str) -> None:
    """A parametric sigma scaled by a long horizon would otherwise say 'lose 140%'."""
    wild = pd.Series(np.random.default_rng(1).normal(0.0, 0.25, 2000), index=hourly_index(2000))
    config = RiskConfig(confidence=0.99, horizon_bars=168, method=method)  # type: ignore[arg-type]

    assert value_at_risk(wild, config) == 1.0
    assert conditional_value_at_risk(wild, config) == 1.0


def test_the_cap_leaves_ordinary_losses_alone() -> None:
    returns = normal_returns()

    assert value_at_risk(returns, RiskConfig(confidence=0.99, horizon_bars=24)) < 1.0
