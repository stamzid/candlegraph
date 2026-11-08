"""The model-based gap estimators: the shapes their uncertainty must have.

`docs/roadmap/DESIGN.md` §0 keeps two forms of the same model for different
consumers. The properties below are what make that split real rather than
nominal: the filter looks only backwards and widens without bound, the
bridge is pinned at both ends and is most unsure in the middle.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from candlegraph.analytics.statespace import (
    MIN_SIGMA,
    GapEstimate,
    brownian_bridge,
    estimate_sigma,
    local_level_forecast,
    log_series,
)

# --- the volatility the band is scaled by ----------------------------------


def test_sigma_is_the_token_s_own_per_hour_return_volatility() -> None:
    rng = np.random.default_rng(0)
    logs = np.cumsum(rng.normal(0.0, 0.02, 4000))

    assert estimate_sigma(logs) == pytest.approx(0.02, rel=0.05)


def test_too_little_history_gives_no_sigma_rather_than_a_guess() -> None:
    """ "We cannot estimate this" is not "the volatility is zero"."""
    assert np.isnan(estimate_sigma(np.array([1.0, 2.0])))
    assert np.isnan(estimate_sigma(np.array([])))


def test_a_flat_series_is_clamped_rather_than_collapsed_to_zero() -> None:
    """A zero band would claim perfect confidence a flat window has not earned."""
    assert estimate_sigma(np.zeros(50)) == MIN_SIGMA


# --- the causal form: the Kalman filter ------------------------------------


def test_the_local_level_forecast_is_flat_because_the_walk_has_no_drift() -> None:
    """A drift term was rejected: a 20-hour extrapolated trend is a fabrication.

    The mean therefore coincides with a forward fill. What the model adds is
    not a better point, it is an honest band around it.
    """
    estimate = local_level_forecast(np.log(100.0), steps=5, sigma=0.01)

    assert estimate.prices() == pytest.approx([100.0] * 5)


def test_the_causal_band_widens_as_the_square_root_of_the_gap() -> None:
    """After twenty hours the model has stopped knowing, and says so."""
    estimate = local_level_forecast(0.0, steps=4, sigma=0.01)

    assert estimate.sigma == pytest.approx(0.01 * np.sqrt([1, 2, 3, 4]))
    assert estimate.sigma[-1] > estimate.sigma[0], "uncertainty never shrinks with distance"


# --- the bridge: pinned at both ends ---------------------------------------


def test_the_bridge_mean_is_a_straight_line_in_log_space() -> None:
    """Geometric, not arithmetic: a 1% move costs the same at 3 and at 30,000."""
    estimate = brownian_bridge(np.log(100.0), np.log(400.0), steps=1, sigma=0.01)

    assert estimate.prices() == pytest.approx([200.0]), "the geometric midpoint, not 250"


def test_the_bridge_band_is_widest_halfway_across_and_zero_at_the_ends() -> None:
    """The shape of an honest "we are least sure in the middle"."""
    estimate = brownian_bridge(0.0, 0.0, steps=5, sigma=0.01)

    assert estimate.sigma[2] == max(estimate.sigma), "widest in the middle"
    assert estimate.sigma[0] == pytest.approx(estimate.sigma[-1]), "symmetric"
    assert estimate.sigma[0] < estimate.sigma[2]
    # Pinned: a one-hour gap between two known bars is barely uncertain at all.
    assert brownian_bridge(0.0, 0.0, steps=1, sigma=0.01).sigma[0] == pytest.approx(0.01 * 0.5**0.5)


def test_the_bridge_is_narrower_than_the_filter_because_it_knows_more() -> None:
    """It reads the bar after the gap. That is the accuracy, and the look-ahead."""
    bridge = brownian_bridge(0.0, 0.0, steps=6, sigma=0.01)
    filtered = local_level_forecast(0.0, steps=6, sigma=0.01)

    assert (bridge.sigma < filtered.sigma).all()


# --- shared behaviour ------------------------------------------------------


@pytest.mark.parametrize("estimator", [brownian_bridge, local_level_forecast])
def test_no_sigma_means_a_mean_with_no_band_rather_than_no_estimate(estimator: object) -> None:
    estimate = (
        brownian_bridge(0.0, 1.0, 3, float("nan"))
        if estimator is brownian_bridge
        else local_level_forecast(0.0, 3, float("nan"))
    )

    assert estimate.steps == 3
    assert np.isnan(estimate.sigma).all()
    assert np.isfinite(estimate.mean).all()


def test_an_empty_gap_produces_an_empty_estimate() -> None:
    assert brownian_bridge(0.0, 1.0, 0, 0.01).steps == 0
    assert local_level_forecast(0.0, 0, 0.01).steps == 0


def test_mean_and_sigma_must_describe_the_same_hours() -> None:
    with pytest.raises(ValueError, match="same hours"):
        GapEstimate(np.zeros(3), np.zeros(2))


def test_a_non_positive_price_is_dropped_rather_than_becoming_minus_infinity() -> None:
    """Letting it through would poison every window that touched it."""
    prices = pd.Series([1.0, 0.0, -5.0, 2.0])

    logs = log_series(prices)

    assert logs.isna().tolist() == [False, True, True, False]
    assert np.isfinite(logs.dropna()).all()
