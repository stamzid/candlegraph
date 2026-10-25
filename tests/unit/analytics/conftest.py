"""Deterministic synthetic price series for the analytics tests.

Real market data would make these tests depend on what the subgraph happened
to return. Every series here is generated from a fixed seed with a known
shape, so an assertion failure means the maths changed, not the market.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

SEED = 20261025


def hourly_index(n: int, start: str = "2026-01-01") -> pd.DatetimeIndex:
    return pd.date_range(start, periods=n, freq="h", tz="UTC")


def constant_series(n: int = 100, value: float = 100.0, name: str = "FLAT") -> pd.Series:
    """A perfectly flat series: zero volatility, undefined z-score."""
    return pd.Series(np.full(n, value), index=hourly_index(n), name=name)


def linear_series(n: int = 100, start: float = 100.0, step: float = 1.0) -> pd.Series:
    """A straight line: trending, with a known mean at every window."""
    return pd.Series(start + step * np.arange(n, dtype="float64"), index=hourly_index(n), name="LINE")


def random_walk(n: int = 500, sigma: float = 0.01, start: float = 100.0, seed: int = SEED) -> pd.Series:
    """A geometric random walk: no mean reversion, no trend."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, sigma, n)
    return pd.Series(start * np.exp(np.cumsum(steps)), index=hourly_index(n), name="WALK")


def mean_reverting(n: int = 600, phi: float = 0.9, sigma: float = 0.02, seed: int = SEED) -> pd.Series:
    """An AR(1) around a constant level: what Bollinger is built to trade."""
    rng = np.random.default_rng(seed)
    x = np.zeros(n)
    for i in range(1, n):
        x[i] = phi * x[i - 1] + rng.normal(0.0, sigma)
    return pd.Series(100.0 * np.exp(x), index=hourly_index(n), name="REVERT")


def trending(n: int = 400, drift: float = 0.001, sigma: float = 0.004, seed: int = SEED) -> pd.Series:
    """A random walk with positive drift: what a crossover is built to trade."""
    rng = np.random.default_rng(seed)
    return pd.Series(
        100.0 * np.exp(np.cumsum(rng.normal(drift, sigma, n))), index=hourly_index(n), name="TREND"
    )


@pytest.fixture
def walk() -> pd.Series:
    return random_walk()


@pytest.fixture
def reverting() -> pd.Series:
    return mean_reverting()


@pytest.fixture
def trend() -> pd.Series:
    return trending()


@pytest.fixture
def two_asset_returns() -> pd.DataFrame:
    """Two assets with known, different risk and a mild positive correlation."""
    rng = np.random.default_rng(SEED)
    n = 2000
    common = rng.normal(0.0, 0.004, n)
    return pd.DataFrame(
        {
            # Low risk, low return.
            "SAFE": 0.00002 + 0.3 * common + rng.normal(0.0, 0.002, n),
            # Higher risk, higher return.
            "RISKY": 0.00010 + 0.8 * common + rng.normal(0.0, 0.012, n),
        },
        index=hourly_index(n),
    )
