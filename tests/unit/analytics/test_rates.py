"""The risk-free rate comes through one seam, and the real rate is the Fisher one."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from candlegraph.analytics.rates import (
    RATE_SOURCES,
    ConstantRateProvider,
    RateQuote,
    RateSourceError,
    rate_provider_for,
    real_rate,
)
from tests.conftest import make_settings


def test_the_real_rate_is_the_fisher_relation_not_the_subtraction() -> None:
    """At 4% nominal and 5% inflation the shortcut says -1.00%; Fisher says -0.95%."""
    assert real_rate(0.04, 0.05) == pytest.approx((1.04 / 1.05) - 1.0)
    assert real_rate(0.04, 0.05) != pytest.approx(-0.01)


def test_zero_inflation_leaves_the_rate_alone() -> None:
    assert real_rate(0.04, 0.0) == pytest.approx(0.04)


def test_inflation_of_minus_one_hundred_percent_has_no_real_rate() -> None:
    with pytest.raises(ValueError, match="inflation"):
        real_rate(0.04, -1.0)


def test_a_quote_exposes_its_real_rate_and_provenance() -> None:
    when = datetime(2026, 10, 30, tzinfo=UTC)
    quote = RateQuote(nominal=0.05, inflation=0.03, source="constant", as_of=when)

    assert quote.real == pytest.approx(real_rate(0.05, 0.03))
    assert quote.source == "constant"
    assert quote.as_of == when


def test_the_constant_provider_returns_the_settings_every_time() -> None:
    provider = ConstantRateProvider(nominal=0.04, inflation=0.02)

    first, second = provider.quote(), provider.quote(datetime(2026, 1, 1, tzinfo=UTC))

    assert provider.name == "constant"
    assert (first.nominal, first.inflation) == (0.04, 0.02)
    assert (second.nominal, second.inflation) == (0.04, 0.02)
    assert second.as_of == datetime(2026, 1, 1, tzinfo=UTC)


def test_settings_build_the_constant_provider_by_default() -> None:
    settings = make_settings(risk_free_rate=0.045, inflation_rate=0.03)

    quote = rate_provider_for(settings).quote()

    assert quote.nominal == pytest.approx(0.045)
    assert quote.inflation == pytest.approx(0.03)
    assert quote.source == "constant"


def test_an_unknown_rate_source_is_refused_with_the_available_names() -> None:
    """`tavily` is reserved for the knowledge layer and does not exist yet; say so."""
    settings = make_settings(risk_free_rate_source="tavily")

    with pytest.raises(RateSourceError, match="tavily") as raised:
        rate_provider_for(settings)
    assert all(name in str(raised.value) for name in RATE_SOURCES)
