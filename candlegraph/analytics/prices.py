"""Loading stored hourly rows into aligned pandas frames.

The only database-touching module in :mod:`candlegraph.analytics`, and it only
reads. Everything downstream takes a frame, so the numeric code stays testable
without a database.

Alignment is the whole job here. Tokens have genuinely different trading
hours — a thin token emits no row for an hour with no swaps — so a naive
join produces a ragged matrix, and a covariance estimated from ragged windows
is not a covariance. :func:`load_price_frame` resolves that explicitly and
says which policy it used.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import numpy as np
import pandas as pd

from candlegraph.storage import DatabaseManager
from candlegraph.tokens import TokenRegistry

PriceColumn = Literal["close", "price_usd", "open", "high", "low"]
FillPolicy = Literal["ffill", "drop", "none", "interpolate", "causal", "bridge"]

HOUR = 3600
# Beyond a day, a carried or interpolated price is fiction rather than a
# reasonable stand-in for an hour nobody traded in. Both filling policies
# share the cap so "how stale may a synthetic bar be" has one answer.
DEFAULT_MAX_FILL = 24


class PriceError(LookupError):
    """There is no usable stored series for the request.

    Carries a message written for a person: an unknown symbol lists the
    tracked ones, an empty window says how long it was. Each surface maps
    it to its own failure — the CLI to ``SystemExit``, the MCP tools to a
    tool error — so the wording is written once.
    """


# --- the fill seam -----------------------------------------------------------
#
# `docs/roadmap/DESIGN.md` §0 settles what may be done about an hour with no
# row, and scopes the answer by *who is asking*. This is that decision as
# code: every policy declares whether it is causal, and the loader a strategy
# uses refuses the one that is not.
#
# Nothing here writes. Filling is a read-time policy — `token_hours_data`
# keeps emitting no row for an hour with no swaps, so coverage stays
# falsifiable (invariants 6 and 11).


@dataclass(frozen=True)
class FillRule:
    """One answer to "what do we do about an hour with no row".

    ``causal`` is the field that matters. A causal rule uses only data at or
    before the bar it fills, so a strategy consuming it cannot see the
    future; a non-causal one reads across the gap and is therefore display
    only. Invariant 17 does not relax for synthetic bars, and the whole
    point of naming the rules is that the distinction stops being a comment.
    """

    name: str
    causal: bool
    summary: str
    caveat: str = ""


FILL_RULES: dict[str, FillRule] = {
    "none": FillRule(
        "none",
        causal=True,
        summary="Leave the hole. The honest default for any statistic computed over aligned bars.",
    ),
    "drop": FillRule(
        "drop",
        causal=True,
        summary="Keep only hours every symbol has.",
        caveat="Strict, but can throw away most of a thin token's history.",
    ),
    "ffill": FillRule(
        "ffill",
        causal=True,
        summary="Carry the last known price forward, capped.",
        caveat="Flattens returns across the gap, so realised volatility is understated.",
    ),
    "interpolate": FillRule(
        "interpolate",
        causal=False,
        summary="Straight line between the last real bar before the gap and the first after it.",
        caveat="Uses the gap's right endpoint — a future price. Display only. A straight line "
        "has zero local variance, so every rolling standard deviation overlapping a filled "
        "stretch is understated: Bollinger bands tighten and z-scores inflate. It also has no "
        "error bar at all: a one-hour fill and a twenty-hour guess look identical.",
    ),
    "causal": FillRule(
        "causal",
        causal=True,
        summary="Kalman local-level forecast: the flat random-walk path, with a band that "
        "widens as sqrt(hours).",
        caveat="Uses only data before the gap, so a strategy may consume it. The mean "
        "coincides with a forward fill; what it adds is the admission of uncertainty.",
    ),
    "bridge": FillRule(
        "bridge",
        causal=False,
        summary="Brownian bridge in log space, pinned to the observed bars either side, with a "
        "band widest halfway across.",
        caveat="Conditions on the bar *after* the gap, so it is display only. More accurate "
        "than either one-sided estimate and the only one that says how unsure it is "
        "where it matters — but the posterior mean is still a smooth path, so realised "
        "volatility over a filled stretch is understated.",
    ),
}


def fill_rule(policy: str) -> FillRule:
    """The rule for ``policy``, or a :class:`PriceError` naming the real ones."""
    try:
        return FILL_RULES[policy]
    except KeyError:
        raise PriceError(f"unknown fill policy {policy!r}; known: {', '.join(sorted(FILL_RULES))}") from None


def is_causal(policy: str) -> bool:
    """Whether ``policy`` may be fed to something that trades."""
    return fill_rule(policy).causal


def require_causal(policy: str, consumer: str) -> None:
    """Refuse a look-ahead fill on behalf of ``consumer``.

    The backtester cannot tell a real bar from one that embeds the future,
    which is exactly why this raises rather than silently downgrading to a
    causal policy: a quiet substitution would make the chosen policy a lie
    and the resulting equity curve unexplainable.

    Raises:
        PriceError: ``policy`` reads across the gap it fills.
    """
    rule = fill_rule(policy)
    if not rule.causal:
        raise PriceError(
            f"fill policy {policy!r} is not causal and {consumer} may not consume it: "
            f"{rule.caveat.split('.')[0]}. Use one of "
            f"{', '.join(sorted(n for n, r in FILL_RULES.items() if r.causal))}."
        )


@dataclass(frozen=True)
class PriceHistory:
    """Per-symbol hourly rows, long format, plus how they were fetched."""

    frame: pd.DataFrame
    symbols: tuple[str, ...]
    start_unix: int | None
    end_unix: int | None

    @property
    def empty(self) -> bool:
        return self.frame.empty

    def for_symbol(self, symbol: str) -> pd.DataFrame:
        """One symbol's rows, indexed by UTC timestamp."""
        rows = self.frame[self.frame["symbol"] == symbol]
        return rows.set_index("timestamp").sort_index()

    def close(self, symbol: str, column: PriceColumn = "price_usd") -> pd.Series:
        """One symbol's price series, indexed by UTC timestamp."""
        series = self.for_symbol(symbol)[column].astype("float64")
        series.name = symbol
        return series


def window_bounds(
    days: int | None = None,
    end: datetime | None = None,
    now: datetime | None = None,
) -> tuple[int | None, int | None]:
    """Unix bounds for a trailing window of ``days``.

    ``None`` days means "everything stored", which is the default for
    analytics: history was kept precisely so it could be used.
    """
    if days is None:
        return None, None
    finish = end or now or datetime.now(tz=UTC)
    return int((finish - timedelta(days=days)).timestamp()), int(finish.timestamp())


def load_price_history(
    db: DatabaseManager,
    symbols: list[str],
    start_unix: int | None = None,
    end_unix: int | None = None,
) -> PriceHistory:
    """Read hourly rows for the given symbols.

    Returns:
        A :class:`PriceHistory`; ``frame`` is empty when nothing is stored
        rather than raising, so callers can report "no data" themselves.
    """
    if not symbols:
        return PriceHistory(_empty_frame(), (), start_unix, end_unix)

    rows = db.fetch_all(
        db.sql.get_price_history,
        {"symbols": list(symbols), "start_unix": start_unix, "end_unix": end_unix},
    )
    frame = _empty_frame() if not rows else _to_frame(rows)
    return PriceHistory(frame, tuple(symbols), start_unix, end_unix)


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": pd.Series(dtype="object"),
            "timestamp": pd.Series(dtype="datetime64[ns, UTC]"),
            "period_start_unix": pd.Series(dtype="int64"),
            "close": pd.Series(dtype="float64"),
            "price_usd": pd.Series(dtype="float64"),
        }
    )


def _to_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    # Everything arrives as Decimal from psycopg2; GraphQL and numpy both
    # want floats, and Decimal silently breaks numpy's vectorised paths.
    for column in ("open", "high", "low", "close", "price_usd", "volume_usd", "total_value_locked_usd"):
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("float64")
    frame["period_start_unix"] = frame["period_start_unix"].astype("int64")
    frame["timestamp"] = pd.to_datetime(frame["period_start_unix"], unit="s", utc=True)
    return frame


@dataclass(frozen=True)
class FilledFrame:
    """A wide price frame and the record of which cells are not observations.

    ``synthetic`` is the same shape as ``frame`` and is ``True`` exactly
    where a value was manufactured by the policy. It travels with the frame
    rather than being recoverable from it, because once a number is in a
    column nothing about it says where it came from — which is the mistake
    the whole seam exists to prevent.
    """

    frame: pd.DataFrame
    synthetic: pd.DataFrame
    policy: str
    sigma: pd.DataFrame = field(default_factory=pd.DataFrame)
    """One standard deviation of each filled cell, in **log** space.

    ``NaN`` everywhere for the policies that have no error bar — which is
    most of them, and is the honest answer rather than a zero. Only the
    model-based estimators (``causal``, ``bridge``) populate it, and that
    asymmetry is the point: a straight line cannot tell you how unsure it is.
    """

    @property
    def has_bands(self) -> bool:
        """Whether this policy produced an uncertainty band at all."""
        return not self.sigma.empty and bool(self.sigma.notna().to_numpy().any())

    @property
    def filled_cells(self) -> int:
        return int(self.synthetic.to_numpy().sum()) if not self.synthetic.empty else 0

    @property
    def rule(self) -> FillRule:
        return fill_rule(self.policy)

    def filled_index(self, symbol: str) -> pd.DatetimeIndex:
        """Timestamps where ``symbol``'s value was manufactured, not observed."""
        if symbol not in self.synthetic.columns:
            return pd.DatetimeIndex([], tz="UTC")
        marked = self.synthetic[symbol]
        return pd.DatetimeIndex(marked.index[marked.to_numpy()])


def run_lengths(mask: pd.Series) -> pd.Series:
    """For each position, the length of the consecutive ``True`` run holding it.

    ``0`` where the mask is ``False``. Pure, and the reason a gap longer than
    the cap is left *entirely* alone: pandas' own ``limit`` would fill the
    first N hours of a 30-hour hole and leave the rest, producing a bar that
    is half line and half absence. The design says the hole stays.
    """
    if mask.empty:
        return pd.Series(dtype="int64", index=mask.index)
    blocks = (mask != mask.shift()).cumsum()
    sizes = mask.groupby(blocks).transform("size")
    return sizes.where(mask, 0).astype("int64")


def _interpolate(wide: pd.DataFrame, max_fill: int | None) -> pd.DataFrame:
    """Straight lines across interior gaps, leaving over-long runs untouched.

    ``limit_area="inside"`` is what stops the line being extrapolated past
    the first or last real bar: a token that started late has a short
    history, not a gap, and inventing prices before it existed would be
    fabrication rather than filling.
    """
    columns = {}
    for name in wide.columns:
        series = wide[name]
        holes = series.isna()
        filled = series.interpolate(method="time", limit_area="inside")
        if max_fill is not None:
            filled = filled.mask(run_lengths(holes) > max_fill)
        columns[name] = filled
    return pd.DataFrame(columns, index=wide.index) if columns else wide.copy()


def _model_fill(wide: pd.DataFrame, policy: str, max_fill: int | None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The state-space estimators, gap by gap. Returns ``(filled, sigma)``.

    Everything happens in log space (see :mod:`analytics.statespace`) and
    comes back as prices. Over-long runs are left alone, exactly as the
    other capped policies leave them.

    The ``causal`` branch estimates volatility from the observations
    **strictly before each gap**, not from the whole column. Using the whole
    series would be a quiet look-ahead: the band — and so anything that
    sized a position by it — would depend on bars that had not happened.
    """
    from candlegraph.analytics.statespace import brownian_bridge, estimate_sigma, local_level_forecast

    filled = wide.copy()
    sigma_frame = pd.DataFrame(np.nan, index=wide.index, columns=wide.columns, dtype="float64")

    for name in wide.columns:
        values = statespace_logs(wide[name]).to_numpy(dtype="float64")
        observed = np.flatnonzero(np.isfinite(values))
        if observed.size < 2:
            continue

        # Built as plain arrays and assigned once per column: positional
        # frame writes inside the loop are both slower and untypeable.
        prices = wide[name].to_numpy(dtype="float64").copy()
        bands = np.full(prices.shape, np.nan)
        whole_sigma = estimate_sigma(values[observed])

        for first, second in itertools.pairwise(observed):
            steps = int(second - first - 1)
            if steps < 1 or (max_fill is not None and steps > max_fill):
                continue

            if policy == "causal":
                # Only what was knowable at the left edge of this gap.
                trailing = values[observed[observed <= first]]
                estimate = local_level_forecast(values[first], steps, estimate_sigma(trailing))
            else:
                estimate = brownian_bridge(values[first], values[second], steps, whole_sigma)

            prices[first + 1 : second] = estimate.prices()
            bands[first + 1 : second] = estimate.sigma

        filled[name] = prices
        sigma_frame[name] = bands

    return filled, sigma_frame


def statespace_logs(prices: pd.Series) -> pd.Series:
    """Log prices for the estimators. Thin wrapper, kept here for one import."""
    from candlegraph.analytics.statespace import log_series

    return log_series(prices)


def apply_fill(
    wide: pd.DataFrame, fill: FillPolicy = "ffill", max_fill: int | None = DEFAULT_MAX_FILL
) -> FilledFrame:
    """Resolve missing hours under one policy, and say which cells it invented.

    Pure: a frame in, a frame plus its synthetic mask and uncertainty out.
    Every caller that needs to tell an observation from an estimate goes
    through here.
    """
    rule = fill_rule(fill)
    missing = wide.isna()
    sigma = pd.DataFrame(np.nan, index=wide.index, columns=wide.columns, dtype="float64")

    match rule.name:
        case "ffill":
            filled = wide.ffill(limit=max_fill)
        case "drop":
            filled = wide.dropna(how="any")
        case "interpolate":
            filled = _interpolate(wide, max_fill)
        case "causal" | "bridge":
            filled, sigma = _model_fill(wide, rule.name, max_fill)
        case _:
            filled = wide.copy()

    # A cell is synthetic when it had no observation and now carries a value.
    # `drop` removes whole rows rather than filling cells, so its mask is all
    # False by construction — which is the truth: nothing was invented.
    was_missing = missing.reindex(index=filled.index, columns=filled.columns, fill_value=False)
    synthetic = was_missing & filled.notna()
    sigma = sigma.reindex(index=filled.index, columns=filled.columns).where(synthetic)
    return FilledFrame(frame=filled, synthetic=synthetic, policy=rule.name, sigma=sigma)


def to_wide(
    history: PriceHistory,
    column: PriceColumn = "price_usd",
    fill: FillPolicy = "ffill",
    max_fill: int | None = DEFAULT_MAX_FILL,
) -> pd.DataFrame:
    """Pivot long rows into one column per symbol on a shared hourly index.

    Args:
        history: Rows from :func:`load_price_history`.
        column: Which price to take.
        fill: How to resolve hours a symbol is missing.
            ``"ffill"`` carries the last known price forward, which is the
            right assumption for an asset that simply did not trade;
            ``"drop"`` keeps only hours every symbol has, which is strict but
            can throw away most of a thin token's history; ``"none"`` leaves
            the holes in place.
        max_fill: Cap on consecutive forward-filled hours. Beyond this the
            last price is stale enough to be misleading, so the hole stays.
            ``None`` fills without limit.

    Returns:
        A frame indexed by UTC timestamp with one float column per symbol,
        reindexed onto a complete hourly grid so gaps are visible as ``NaN``
        rather than silently closed up.
    """
    return to_wide_filled(history, column=column, fill=fill, max_fill=max_fill).frame


def to_wide_filled(
    history: PriceHistory,
    column: PriceColumn = "price_usd",
    fill: FillPolicy = "ffill",
    max_fill: int | None = DEFAULT_MAX_FILL,
) -> FilledFrame:
    """:func:`to_wide`, keeping the record of which cells were manufactured.

    The same pivot and the same policy; the only difference is that the
    synthetic mask survives the call. Display code that renders a filled bar
    differently from an observed one needs this one, not :func:`to_wide`.
    """
    if history.empty:
        empty = pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC", name="timestamp"))
        return FilledFrame(frame=empty, synthetic=empty.copy(), policy=fill_rule(fill).name)

    wide = history.frame.pivot_table(
        index="timestamp", columns="symbol", values=column, aggfunc="last"
    ).sort_index()
    wide.columns.name = None

    # A complete grid makes a missing hour explicit instead of letting the
    # index skip it, which would make returns span a gap without saying so.
    full_index = pd.date_range(wide.index.min(), wide.index.max(), freq="h", tz="UTC")
    wide = wide.reindex(full_index)
    wide.index.name = "timestamp"

    result = apply_fill(wide.astype("float64"), fill=fill, max_fill=max_fill)
    return FilledFrame(frame=result.frame.astype("float64"), synthetic=result.synthetic, policy=result.policy)


def load_price_frame(
    db: DatabaseManager,
    symbols: list[str],
    days: int | None = None,
    column: PriceColumn = "price_usd",
    fill: FillPolicy = "ffill",
    now: datetime | None = None,
) -> pd.DataFrame:
    """Load a wide price frame in one call. The common entry point."""
    start_unix, end_unix = window_bounds(days, now=now)
    history = load_price_history(db, symbols, start_unix, end_unix)
    return to_wide(history, column=column, fill=fill)


def load_symbol_series(
    db: DatabaseManager,
    registry: TokenRegistry,
    symbol: str,
    days: int | None = None,
    column: PriceColumn = "price_usd",
    fill: FillPolicy = "ffill",
    now: datetime | None = None,
) -> pd.Series:
    """One tracked token's price series, or a :class:`PriceError` saying why not.

    The registry lookup is case-insensitive but the stored ``symbol`` column
    is not, so the query always uses the registry's own spelling.

    **This is the loader a strategy, a backtest and a signal run use**, so it
    refuses a non-causal fill policy outright. A display surface that wants
    interpolation calls :func:`to_wide_filled` and keeps the synthetic mask
    with the numbers; there is no way to get a look-ahead fill into a
    strategy through this function, which is the point of the seam.

    Raises:
        PriceError: the symbol is not tracked, nothing is stored for it
            inside the window, or ``fill`` reads across the gaps it fills.
    """
    require_causal(fill, "a strategy or backtest")
    token = registry.by_symbol(symbol)
    if token is None:
        raise PriceError(f"unknown token {symbol!r}; tracked: {', '.join(registry.symbols)}")
    frame = load_price_frame(db, [token.symbol], days=days, column=column, fill=fill, now=now)
    if frame.empty or token.symbol not in frame.columns or frame[token.symbol].dropna().empty:
        window = "the stored history" if days is None else f"the last {days} days"
        raise PriceError(f"no stored price history for {token.symbol} in {window}")
    series = frame[token.symbol].dropna()
    series.name = token.symbol
    return series
