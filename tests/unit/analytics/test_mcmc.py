"""Posteriors by Metropolis-Hastings, and the diagnostics that gate them.

An unconverged chain produces confident nonsense and, unlike a failed
optimiser, does not announce itself. So most of what is tested here is the
*refusal* path: that R-hat and ESS are computed, that `usable` is false
when they fail, and that a sampler which cannot run says so.

The one statistical claim worth pinning is that the posterior interval is
wider than the point-estimate one — and that it stops being wider as the
sample grows, because that is the method agreeing with the point estimate
rather than failing.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from candlegraph.analytics.mcmc import (
    MAX_R_HAT,
    MIN_ESS,
    MCMCError,
    effective_sample_size,
    format_posterior,
    gelman_rubin,
    posterior_forecast,
    random_walk_log_density,
    sample,
)

TRUE_MU = 0.0003
TRUE_SIGMA = 0.012


def walk(n: int = 600, seed: int = 1) -> pd.Series:
    rng = np.random.default_rng(seed)
    return pd.Series(100.0 * np.exp(np.cumsum(rng.normal(TRUE_MU, TRUE_SIGMA, n))))


def gaussian_density(mean: float = 0.0, sd: float = 1.0) -> object:
    def density(params: np.ndarray) -> float:
        return float(-0.5 * np.sum(((params - mean) / sd) ** 2))

    return density


# --- the diagnostics -----------------------------------------------------------


def test_chains_that_agree_give_an_r_hat_near_one() -> None:
    rng = np.random.default_rng(0)
    agreeing = rng.normal(0.0, 1.0, size=(4, 2000, 1))

    assert gelman_rubin(agreeing).max() < MAX_R_HAT


def test_chains_stuck_in_different_places_are_caught() -> None:
    """The failure R-hat exists for: each chain looks fine alone."""
    rng = np.random.default_rng(0)
    apart = np.stack([rng.normal(offset, 0.1, size=(500, 1)) for offset in (-5.0, 0.0, 5.0)])

    assert gelman_rubin(apart).max() > 2.0


def test_one_chain_cannot_produce_an_r_hat() -> None:
    """It compares chains, so a single chain is not a weaker answer but no
    answer — reported as infinity so it fails the gate rather than passing."""
    assert np.isinf(gelman_rubin(np.zeros((1, 100, 2)))).all()


def test_independent_draws_are_worth_about_their_own_number() -> None:
    rng = np.random.default_rng(0)
    independent = rng.normal(size=(2, 3000))

    assert effective_sample_size(independent) > 3000, "6000 draws, little autocorrelation"


def test_a_sticky_chain_is_worth_far_less_than_its_length() -> None:
    """A chain that moves rarely has a hundred draws worth ten."""
    rng = np.random.default_rng(0)
    sticky = np.cumsum(rng.normal(size=(2, 3000)), axis=1)  # a random walk: highly correlated

    assert effective_sample_size(sticky) < 500


# --- the sampler ----------------------------------------------------------------


def test_the_sampler_recovers_a_known_distribution() -> None:
    """Metropolis on a standard normal should return a standard normal."""
    drawn = sample(
        gaussian_density(mean=2.0, sd=1.0),  # type: ignore[arg-type]
        initial=np.array([0.0]),
        names=("x",),
        draws=4000,
        warmup=1500,
        chains=4,
        seed=1,
    )

    assert drawn.usable, drawn.diagnosis
    assert float(np.mean(drawn.flat)) == pytest.approx(2.0, abs=0.1)
    assert float(np.std(drawn.flat)) == pytest.approx(1.0, abs=0.1)


def test_parameters_on_very_different_scales_still_mix() -> None:
    """The bug this caught: one scalar tuning factor cannot fix a proposal
    four times too narrow in one parameter and twenty times too wide in
    another, which is exactly a drift near 1e-4 beside a log volatility
    near -4. The proposal is re-fitted per dimension during warm-up."""
    returns = np.random.default_rng(2).normal(TRUE_MU, TRUE_SIGMA, 600)

    drawn = sample(
        random_walk_log_density(returns),
        initial=np.array([float(np.mean(returns)), float(np.log(np.std(returns, ddof=1)))]),
        names=("mu", "log_sigma"),
        draws=1500,
        warmup=800,
        chains=4,
        seed=3,
    )

    assert drawn.usable, drawn.diagnosis
    assert drawn.ess.min() >= MIN_ESS, "both parameters must mix, not just the easy one"
    summary = drawn.summary()
    assert summary["log_sigma"]["p5"] < np.log(TRUE_SIGMA) < summary["log_sigma"]["p95"]


def test_the_seed_makes_the_run_reproducible() -> None:
    first = sample(gaussian_density(), np.array([0.0]), ("x",), draws=300, warmup=200, seed=5)  # type: ignore[arg-type]
    again = sample(gaussian_density(), np.array([0.0]), ("x",), draws=300, warmup=200, seed=5)  # type: ignore[arg-type]

    assert np.array_equal(first.draws, again.draws)
    assert first.seed == 5


def test_too_few_chains_is_refused_because_r_hat_needs_two() -> None:
    with pytest.raises(MCMCError, match="at least 2 chains"):
        sample(gaussian_density(), np.array([0.0]), ("x",), chains=1)  # type: ignore[arg-type]


def test_a_start_the_prior_rejects_is_refused_early() -> None:
    def impossible(params: np.ndarray) -> float:
        return float("-inf")

    with pytest.raises(MCMCError, match="zero posterior density"):
        sample(impossible, np.array([0.0]), ("x",), chains=2)


# --- the forecast -----------------------------------------------------------------


def test_the_posterior_interval_is_wider_on_a_short_window() -> None:
    """The whole point: a point estimate's interval covers innovation noise
    only, and on a short window parameter uncertainty is not negligible."""
    forecast = posterior_forecast(walk(n=60), steps=24, draws=1200, warmup=600, seed=3)

    assert forecast.usable, forecast.posterior.diagnosis
    assert forecast.widening > 0.05, "parameter uncertainty must show up"
    assert forecast.lower < forecast.plugin_lower
    assert forecast.upper > forecast.plugin_upper


def test_the_posterior_agrees_with_the_point_estimate_on_a_long_window() -> None:
    """Not a failure: with enough data the parameters are pinned down, so
    the two intervals converge. A method that stayed wide forever would be
    wrong in the other direction."""
    forecast = posterior_forecast(walk(n=800), steps=24, draws=1200, warmup=600, seed=3)

    assert forecast.usable, forecast.posterior.diagnosis
    assert abs(forecast.widening) < 0.05


def test_the_interval_brackets_the_median_and_the_last_price_is_kept() -> None:
    forecast = posterior_forecast(walk(n=300), steps=12, draws=800, warmup=400, seed=1)

    assert forecast.lower < forecast.median < forecast.upper
    assert forecast.last_price == pytest.approx(float(walk(n=300).iloc[-1]))
    assert forecast.steps == 12


def test_too_little_history_is_refused_rather_than_sampled() -> None:
    with pytest.raises(MCMCError, match="usable returns"):
        posterior_forecast(walk(n=12))


def test_a_nonsense_confidence_is_refused() -> None:
    with pytest.raises(MCMCError, match="confidence"):
        posterior_forecast(walk(n=200), confidence=1.5)


def test_the_printed_report_states_the_diagnostics_and_the_seed() -> None:
    text = format_posterior(posterior_forecast(walk(n=300), steps=12, draws=800, warmup=400, seed=9))

    assert "seed 9" in text
    assert "R-hat" in text and "ESS" in text
    assert "point-estimate interval" in text
    assert "Converged" in text


def test_an_unconverged_run_is_labelled_a_refusal() -> None:
    """Too few draws to mix: the report has to say so rather than print
    percentiles that look like an answer."""
    drawn = sample(
        gaussian_density(),
        np.array([0.0]),
        ("x",),
        draws=5,
        warmup=0,
        chains=2,
        seed=1,  # type: ignore[arg-type]
    )

    assert not drawn.usable
    assert "R-hat" in drawn.diagnosis or "effective sample size" in drawn.diagnosis
