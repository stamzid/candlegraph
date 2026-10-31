"""Where the risk-free rate comes from, and what a *real* rate is.

Every Sharpe ratio, Sortino ratio and tangency portfolio in this package takes
an annual risk-free rate. Today that number is a setting (``RISK_FREE_RATE``),
which is honest but static. Stage 1 brings Tavily into the stack, and a web
search can fetch a current Treasury bill yield and a CPI print; this module is
the seam that lets that happen without touching any caller.

The seam is a :class:`RateProvider` Protocol with one method, ``quote()``,
returning a :class:`RateQuote` that carries the nominal rate, the inflation
rate, where both came from, and when. The only provider that exists is the
constant one, built from settings. A Tavily-backed provider is a later step
and plugs in behind ``RISK_FREE_RATE_SOURCE``.

Nominal versus real. A Sharpe ratio is conventionally computed against the
**nominal** short rate, because that is the yield a portfolio could actually
have earned instead of taking risk; that convention is kept. The **real** rate
(what the money is worth after inflation) is reported beside it, because a 4%
bill yield under 5% inflation is a loss in purchasing power and a reader
deciding whether a strategy's return is worth having should see that. The
conversion is the Fisher relation, exact rather than the ``nominal -
inflation`` shortcut.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from candlegraph.settings import Settings

# The sources `RISK_FREE_RATE_SOURCE` may name. `tavily` is reserved for the
# knowledge-layer provider (docs/analytics/risk/DESIGN.md) and is not built.
RATE_SOURCES: tuple[str, ...] = ("constant",)


class RateSourceError(ValueError):
    """``RISK_FREE_RATE_SOURCE`` names a provider that does not exist."""


def real_rate(nominal: float, inflation: float) -> float:
    """The real rate implied by a nominal rate and an inflation rate (Fisher).

    ``(1 + nominal) / (1 + inflation) - 1``. At low rates this is close to
    ``nominal - inflation``; at crypto-era rates the shortcut is off by
    tenths of a percent, which is why the exact form is used.

    Raises:
        ValueError: inflation of -100% or worse, which has no real rate.
    """
    if inflation <= -1.0:
        raise ValueError("inflation must be greater than -100%")
    return (1.0 + nominal) / (1.0 + inflation) - 1.0


@dataclass(frozen=True)
class RateQuote:
    """An annual risk-free rate, its inflation counterpart, and their provenance.

    Attributes:
        nominal: Annual nominal risk-free rate, e.g. ``0.04``.
        inflation: Annual inflation rate used to derive the real rate.
        source: Which provider produced it (``constant`` today).
        as_of: When the quote applies; ``None`` for a static setting.
    """

    nominal: float
    inflation: float = 0.0
    source: str = "constant"
    as_of: datetime | None = None

    @property
    def real(self) -> float:
        """The inflation-adjusted rate, via :func:`real_rate`."""
        return real_rate(self.nominal, self.inflation)


class RateProvider(Protocol):
    """Anything that can say what the risk-free rate is.

    Implementations must be side-effect free from the caller's point of view:
    ``quote`` may read a cache or a table, but the analytics that consume the
    quote stay pure (invariant 18).
    """

    @property
    def name(self) -> str: ...

    def quote(self, as_of: datetime | None = None) -> RateQuote: ...


@dataclass(frozen=True)
class ConstantRateProvider:
    """The rate from settings, every time. What runs today."""

    nominal: float
    inflation: float = 0.0

    @property
    def name(self) -> str:
        return "constant"

    def quote(self, as_of: datetime | None = None) -> RateQuote:
        return RateQuote(nominal=self.nominal, inflation=self.inflation, source=self.name, as_of=as_of)


def rate_provider_for(settings: Settings) -> RateProvider:
    """Build the configured provider.

    Raises:
        RateSourceError: the configured source is not one that exists, with
            the available names in the message.
    """
    source = settings.risk_free_rate_source.strip().lower()
    if source == "constant":
        return ConstantRateProvider(nominal=settings.risk_free_rate, inflation=settings.inflation_rate)
    raise RateSourceError(
        f"unknown RISK_FREE_RATE_SOURCE {settings.risk_free_rate_source!r}; "
        f"available: {', '.join(RATE_SOURCES)} (a Tavily-backed source is planned, not built)"
    )
