"""Pairwise comparison: alignment semantics, known statistics, honest gaps."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from candlegraph.analytics.pairwise import (
    align_pair,
    annualised_volatility,
    compare_pair,
    format_comparison,
)
from tests.unit.analytics.conftest import SEED, hourly_index, random_walk

# --- alignment ---------------------------------------------------------------


def test_alignment_keeps_only_hours_both_tokens_have() -> None:
    index = hourly_index(10)
    a = pd.Series(np.arange(10, dtype="float64") + 100, index=index, name="A")
    b = pd.Series(np.arange(10, dtype="float64") + 200, index=index, name="B")
    # B is a thin token: it traded in only half the hours.
    b.iloc[[1, 3, 5, 7, 9]] = np.nan

    aligned = align_pair(a, b)
    assert len(aligned) == 5
    assert not aligned.isna().any().any()


def test_a_gap_on_either_side_drops_the_bar_rather_than_filling_it() -> None:
    index = hourly_index(8)
    a = pd.Series(np.full(8, 100.0), index=index, name="A")
    b = pd.Series(np.full(8, 50.0), index=index, name="B")
    a.iloc[2] = np.nan
    b.iloc[5] = np.nan

    aligned = align_pair(a, b)
    assert len(aligned) == 6
    assert index[2] not in aligned.index
    assert index[5] not in aligned.index


def test_disjoint_histories_fail_with_a_clear_message() -> None:
    a = pd.Series([1.0, 2.0, 3.0], index=hourly_index(3, "2026-01-01"), name="A")
    b = pd.Series([1.0, 2.0, 3.0], index=hourly_index(3, "2026-06-01"), name="B")
    with pytest.raises(ValueError, match="aligned bars"):
        compare_pair(a, b)


# --- known statistics --------------------------------------------------------


def test_a_series_against_itself_scaled_has_correlation_and_beta_of_one() -> None:
    walk = random_walk(300)
    scaled = (walk * 2.5).rename("SCALED")
    result = compare_pair(walk, scaled, window=24)

    assert result.correlation == pytest.approx(1.0)
    assert result.spearman == pytest.approx(1.0)
    # Log returns are scale-invariant, so beta of a scaled copy is exactly 1.
    assert result.beta == pytest.approx(1.0)
    assert result.tracking_error == pytest.approx(0.0, abs=1e-12)


def test_independent_walks_are_nearly_uncorrelated() -> None:
    a = random_walk(2000, seed=SEED)
    b = random_walk(2000, seed=SEED + 1).rename("WALK2")
    result = compare_pair(a, b, window=24)
    assert abs(result.correlation) < 0.1


def test_beta_doubles_when_returns_double() -> None:
    base = random_walk(500)
    # A series whose log returns are exactly twice the base's.
    doubled = pd.Series(base.iloc[0] * (base / base.iloc[0]) ** 2.0, index=base.index, name="LEVERED")
    result = compare_pair(doubled, base, window=24)
    assert result.beta == pytest.approx(2.0)


def test_beta_against_a_flat_series_is_undefined_not_infinite() -> None:
    walk = random_walk(100)
    flat = pd.Series(np.full(100, 42.0), index=walk.index, name="FLAT")
    result = compare_pair(walk, flat, window=24)
    assert np.isnan(result.beta)


def test_relative_return_is_the_difference_in_growth_factors() -> None:
    index = hourly_index(4)
    a = pd.Series([100.0, 105.0, 110.0, 120.0], index=index, name="A")  # +20%
    b = pd.Series([50.0, 51.0, 52.0, 55.0], index=index, name="B")  # +10%
    result = compare_pair(a, b, window=2)
    assert result.total_return_a == pytest.approx(0.20)
    assert result.total_return_b == pytest.approx(0.10)
    assert result.relative_return == pytest.approx(0.10)


# --- rolling correlation -----------------------------------------------------


def test_rolling_correlation_is_nan_until_the_first_complete_window() -> None:
    a = random_walk(100)
    b = random_walk(100, seed=SEED + 7).rename("WALK2")
    result = compare_pair(a, b, window=24)

    rolling = result.frame["rolling_correlation"]
    # One bar is lost to the return diff, then 24 returns fill the window.
    assert rolling.iloc[:24].isna().all()
    assert rolling.iloc[25:].notna().all()


# --- helpers -----------------------------------------------------------------


def test_annualised_volatility_needs_two_observations() -> None:
    lone = pd.Series([0.01], index=hourly_index(1))
    assert annualised_volatility(lone) == 0.0


def test_the_formatted_report_names_both_symbols_and_the_bar_count() -> None:
    a = random_walk(50)
    b = (random_walk(50, seed=SEED + 3) * 3).rename("OTHER")
    text = format_comparison(compare_pair(a, b, window=12))
    assert "WALK vs OTHER: 50 aligned bars" in text
    assert "beta (WALK on OTHER)" in text
    assert "tracking error" in text
